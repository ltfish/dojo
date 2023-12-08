import collections
import datetime
import os
import time

import dateparser
from flask import Blueprint, render_template, request, abort
from sqlalchemy import and_, cast
from CTFd.models import db, Challenges, Solves, Users
from CTFd.utils import get_config
from CTFd.utils.user import get_current_user, is_admin
from CTFd.utils.decorators import authed_only, admins_only
from CTFd.cache import cache
from CTFd.plugins import bypass_csrf_protection
from werkzeug.utils import secure_filename

from .discord import get_discord_user
from ..models import DiscordUsers, DojoChallenges, DojoUsers, DojoStudents, DojoModules, DojoStudents, ApprovedCTFs, CTFWriteupSubmission
from ..utils import module_visible, module_challenges_visible, DOJOS_DIR, is_dojo_admin
from ..utils.dojo import dojo_route
from .writeups import WriteupComments, writeup_weeks, all_writeups


course = Blueprint("course", __name__)


def grade(dojo, users_query):
    if isinstance(users_query, Users):
        users_query = Users.query.filter_by(id=users_query.id)

    assessments = dojo.course["assessments"]

    assessment_dates = collections.defaultdict(lambda: collections.defaultdict(dict))
    for assessment in assessments:
        if assessment["type"] not in ["checkpoint", "due"]:
            continue
        assessment["extensions"] = {
            int(user_id): days
            for user_id, days in assessment.get("extensions", {}).items()
        }
        assessment_dates[assessment["id"]][assessment["type"]] = (
            datetime.datetime.fromisoformat(assessment["date"]).astimezone(datetime.timezone.utc),
            assessment["extensions"],
        )

    def dated_count(label, date_type):
        if date_type is None:
            query = lambda module_id: True
        else:
            def query(module_id):
                if date_type not in assessment_dates[module_id]:
                    return None
                date, extensions = assessment_dates[module_id][date_type]
                user_date = db.case(
                    [(Solves.user_id == user_id, date + datetime.timedelta(days=days))
                     for user_id, days in extensions.items()],
                    else_=date
                ) if extensions else date
                return Solves.date < user_date
        return db.func.sum(
            db.case([(DojoModules.id == module_id, cast(query(module_id), db.Integer))
                     for module_id in assessment_dates],
                    else_=None)
        ).label(label)

    solves = (
        dojo
        .solves(ignore_visibility=True)
        .join(DojoModules, and_(
            DojoModules.dojo_id == DojoChallenges.dojo_id,
            DojoModules.module_index == DojoChallenges.module_index,
        ))
        .group_by(Solves.user_id, DojoModules.id)
        .order_by(Solves.user_id, DojoModules.module_index)
        .with_entities(
            Solves.user_id,
            DojoModules.id.label("module_id"),
            dated_count("checkpoint_solves", "checkpoint"),
            dated_count("due_solves", "due"),
            dated_count("all_solves", None)
        )
    ).subquery()
    user_solves = (
        users_query
        .join(solves, Users.id == solves.c.user_id, isouter=True)
        .with_entities(Users.id, *(column for column in solves.c if column.name != "user_id"))
    )

    module_names = {module.id: module.name for module in dojo.modules}
    challenge_counts = {module.id: len(module.challenges) for module in dojo.modules}

    module_solves = {}
    assigmments = {}

    def get_ctf_experience_progress(user_id) -> int:
        the_user = Users.query.filter_by(id=user_id).first()
        submissions = CTFWriteupSubmission.query.filter_by(user=the_user).count()
        # TODO: cross-check to ensure they are not marked as bad submissions
        return submissions

    def get_ctf_experience_credit(user_id) -> float:
        submissions = get_ctf_experience_progress(user_id)
        scores = {
            0: 0.0,
            1: 1.0,
            2: 2.0,
            3: 4.0,
            4: 7.0,
            5: 12.0,
            6: 20.0,
        }
        score = scores.get(submissions, 20.0) / 20
        return score

    def result(user_id):
        grades = []

        for assessment in assessments:
            type = assessment.get("type")

            if type == "checkpoint":
                module_id = assessment["id"]
                module_name = module_names.get(module_id)
                if not module_name:
                    continue

                challenge_count = challenge_counts[module_id]
                checkpoint_solves, due_solves, all_solves = module_solves.get(module_id, (0, 0, 0))
                percent_required = assessment.get("percent_required", 0.334)
                challenge_count_required = int(challenge_count * percent_required)

                date = datetime.datetime.fromisoformat(assessment["date"])
                extension = assessment.get("extensions", {}).get(user_id, 0)
                user_date = date + datetime.timedelta(days=extension)

                grades.append(dict(
                    name=f"{module_name} Checkpoint",
                    date=str(user_date) + (" *" if extension else ""),
                    weight=assessment["weight"],
                    progress=f"{checkpoint_solves} / {challenge_count_required}",
                    credit=bool(checkpoint_solves // (challenge_count_required)),
                ))

            if type == "due":
                module_id = assessment["id"]
                module_name = module_names.get(module_id)
                if not module_name:
                    continue

                challenge_count = challenge_counts[module_id]
                checkpoint_solves, due_solves, all_solves = module_solves.get(module_id, (0, 0, 0))
                late_solves = all_solves - due_solves
                percent_required = assessment.get("percent_required", 1.0)
                challenge_count_required = int(challenge_count * percent_required)

                date = datetime.datetime.fromisoformat(assessment["date"])
                extension = assessment.get("extensions", {}).get(user_id, 0)
                user_date = date + datetime.timedelta(days=extension)

                late_penalty = assessment.get("late_penalty", 0.0)
                late_value = 1 - late_penalty

                grades.append(dict(
                    name=f"{module_name}",
                    date=str(user_date) + (" *" if extension else ""),
                    weight=assessment["weight"],
                    progress=(f"{due_solves} (+{late_solves}) / {challenge_count_required}"
                              if late_solves else f"{due_solves} / {challenge_count_required}"),
                    credit=min((due_solves + late_value * late_solves) / challenge_count_required, 1.0),
                ))

            if type == "manual":
                grades.append(dict(
                    name=assessment["name"],
                    weight=assessment["weight"],
                    progress=assessment.get("progress", {}).get(str(user_id), ""),
                    credit=assessment.get("credit", {}).get(str(user_id), 0.0),
                ))

            if type == "extra":
                grades.append(dict(
                    name=assessment["name"],
                    progress=assessment.get("progress", {}).get(str(user_id), ""),
                    credit=assessment.get("credit", {}).get(str(user_id), 0.0),
                ))

        grades.append({
            "name": "CTF Experience",
            "weight": 20,
            "progress": f"{get_ctf_experience_progress(user_id)} / 6",
            "credit": get_ctf_experience_credit(user_id),
        })

        overall_grade = (
            sum(grade["credit"] * grade["weight"] for grade in grades if "weight" in grade) /
            sum(grade["weight"] for grade in grades if "weight" in grade)
        )
        extra_credit = (
            sum(grade["credit"] for grade in grades if "weight" not in grade)
        )
        overall_grade += extra_credit

        for grade, min_score in dojo.course["letter_grades"].items():
            if overall_grade >= min_score:
                letter_grade = grade
                break
        else:
            letter_grade = "?"

        return dict(user_id=user_id,
                    grades=grades,
                    overall_grade=overall_grade,
                    letter_grade=letter_grade)

    user_id = None
    previous_user_id = None
    for user_id, module_id, checkpoint_solves, due_solves, all_solves in user_solves:
        if user_id != previous_user_id:
            if previous_user_id is not None:
                yield result(previous_user_id)
                module_solves = {}
            previous_user_id = user_id
        if module_id is not None:
            module_solves[module_id] = (
                int(checkpoint_solves) if checkpoint_solves is not None else 0,
                int(due_solves) if due_solves is not None else 0,
                int(all_solves) if all_solves is not None else 0,
            )
    if user_id:
        yield result(user_id)


@course.route("/dojo/<dojo>/course")
@course.route("/dojo/<dojo>/course/<resource>")
@dojo_route
def view_course(dojo, resource=None):
    if not dojo.course:
        abort(404)

    if request.args.get("user"):
        if not dojo.is_admin():
            abort(403)
        user = Users.query.filter_by(id=request.args.get("user")).first_or_404()
        name = f"{user.name}'s"
    else:
        user = get_current_user()
        name = "Your"

    current_user = get_current_user()
    is_admin = False if current_user is None else (current_user.type == "admin")

    grades = {}
    identity = {}

    setup = {
        step: "incomplete"
        for step in ["create_account", "link_student", "create_discord", "link_discord", "join_discord"]
    }

    if user:
        grades = next(grade(dojo, user))

        student = DojoStudents.query.filter_by(dojo=dojo, user=user).first()
        identity["identity_name"] = dojo.course.get("student_id", "Identity")
        identity["identity_value"] = student.token if student else None

        setup["create_account"] = "complete"

        if student and student.token in dojo.course.get("students", []):
            setup["link_student"] = "complete"
        elif student:
            setup["link_student"] = "unknown"

        if DiscordUsers.query.filter_by(user=user).first():
            setup["create_discord"] = "complete"
            setup["link_discord"] = "complete"

        cache.delete_memoized(get_discord_user, user.id)
        if get_discord_user(user.id):
            setup["join_discord"] = "complete"
        else:
            setup["join_discord"] = "incomplete"

    # select all active CTFs
    ctfs = []
    for approved_ctf in ApprovedCTFs.query.filter(ApprovedCTFs.flag_submission_due >= datetime.datetime.now()).all():
        ctf = {
            "id": approved_ctf.ctf_id,
            "name": approved_ctf.name,
        }
        ctfs.append(ctf)

    return render_template("course.html", name=name, **grades, **identity, **setup, user=user, dojo=dojo, approved_ctfs=ctfs, admin=is_admin)


@course.route("/submit_ctf_writeup", methods=["POST"])
@bypass_csrf_protection
@authed_only
def submit_ctf_writeup():
    BASE_DIR = "/var/uploads"

    user = get_current_user()

    ctf_id = request.form.get("ctfs", None)
    team_name = request.form.get("team-name", "")
    challenge_name = request.form.get("challenge-name", "")
    flag = request.form.get("flag", "")
    solved_after_ctf = request.form.get("solved-after_ctf", False)
    writeup = request.files.get("writeup-zip", None)

    # TODO: Sanity checks
    if not ctf_id:
        return {"success": False, "error": "Invalid CTF ID"}
    try:
        ctf_id = int(ctf_id)
    except ValueError:
        return {"success": False, "error": "Invalid CTF ID"}

    team_name = team_name.strip(" ")
    if not team_name:
        return {"success": False, "error": "Invalid team name"}

    challenge_name = challenge_name.strip(" ")
    if not challenge_name:
        return {"success": False, "error": "Invalid challenge name"}

    flag = flag.strip(" ")
    if not flag:
        return {"success": False, "error": "Invalid flag"}

    if solved_after_ctf is not False:
        solved_after_ctf = True

    if not writeup or not writeup.filename:
        return {"success": False, "error": "Please upload a writeup file"}

    # has a writeup been submitted for this challenge?
    submitted = CTFWriteupSubmission.query.filter_by(user=user, ctf_id=ctf_id, challenge_name=challenge_name).count()
    if submitted > 0:
        return {"success": False, "error": "You have already submitted a writeup for this challenge"}

    allowed_suffix = [
        ".pdf",
        ".zip",
        ".tar",
        ".tar.gz",
        ".7z",
        ".txt",
    ]
    allowed = False
    for suffix in allowed_suffix:
        if writeup.filename.endswith(suffix):
            allowed = True
    if not allowed:
        return {
            "success": False,
            "error": "The name of your upload file must end with one of the following suffixes: " +
                     ", ".join(allowed_suffix)
        }

    # save the uploaded file
    filename = secure_filename(writeup.filename)
    filename = f"{user.id}_{int(time.time())}_{filename}"
    writeup.save(os.path.join(BASE_DIR, filename))

    # create the entry
    submission = CTFWriteupSubmission(
        user=user,
        ctf_id=ctf_id,
        team_name=team_name,
        challenge_name=challenge_name,
        flag=flag,
        solved_after_ctf=solved_after_ctf,
        writeup_path=filename,
    )
    db.session.add(submission)
    db.session.commit()

    return {"success": True }


@course.route("/dojo/<dojo>/course/add_ctf", methods=["PATCH"])
@dojo_route
@admins_only
def add_approved_ctf(dojo):
    ctf_name = request.json.get("name")
    start_time = request.json.get("start_time")
    end_time = request.json.get("end_time")
    flag_submission_due = request.json.get("submission_due")

    start_time = dateparser.parse(start_time)
    end_time = dateparser.parse(end_time)
    flag_submission_due = dateparser.parse(flag_submission_due)

    ctf = ApprovedCTFs(name=ctf_name, start_time=start_time, end_time=end_time, flag_submission_due=flag_submission_due)

    db.session.add(ctf)
    db.session.commit()

    return {"success": True}


@course.route("/dojo/<dojo>/course/identity", methods=["PATCH"])
@dojo_route
@authed_only
def update_identity(dojo):
    user = get_current_user()
    dojo_user = DojoUsers.query.filter_by(dojo=dojo, user=user).first()

    if dojo_user and dojo_user.type == "admin":
        return {"success": False, "error": "Cannot identify admin"}

    identity = request.json.get("identity", None)
    if not dojo_user:
        dojo_user = DojoStudents(dojo=dojo, user=user, token=identity)
        db.session.add(dojo_user)
    else:
        dojo_user.type = "student"
        dojo_user.token = identity
    db.session.commit()

    return {"success": True}


@course.route("/dojo/<dojo>/admin/grades")
@dojo_route
@authed_only
def view_all_grades(dojo):
    if not dojo.course:
        abort(404)

    if not dojo.is_admin():
        abort(403)

    users = (
        Users
        .query
        .join(DojoStudents, DojoStudents.user_id == Users.id)
        .filter(DojoStudents.dojo == dojo)
    )
    grades = grade(dojo, users)

    return render_template("grades_admin.html", grades=grades)
