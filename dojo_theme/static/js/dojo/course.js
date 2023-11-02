var error_template =
    '<div class="alert alert-danger alert-dismissable" role="alert">\n' +
    '  <span class="sr-only">Error:</span>\n' +
    '  <span id="message"></span>' +
    '  <button type="button" class="close" data-dismiss="alert" aria-label="Close"><span aria-hidden="true">×</span></button>\n' +
    '</div>';

var success_template =
    '<div class="alert alert-success alert-dismissable submit-row" role="alert">\n' +
    '  <strong>Success!</strong>\n' +
    '  <span id="message"></span>' +
    '  <button type="button" class="close" data-dismiss="alert" aria-label="Close"><span aria-hidden="true">×</span></button>\n' +
    '</div>';

function form_fetch_and_show(name, endpoint, method, success_message) {
    const form = $(`#${name}-form`);
    const results = $(`#${name}-results`);
    form.submit(e => {
        e.preventDefault();
        results.empty();
        const params = form.serializeJSON();

        CTFd.fetch(endpoint, {
            method: method,
            credentials: "same-origin",
            headers: {
                Accept: "application/json",
                "Content-Type": "application/json"
            },
            body: JSON.stringify(params)
        }).then(response => {
            return response.json()
        }).then(result => {
            if (result.success) {
                results.html(success_template);
                results.find("#message").text(success_message);
            } else {
                results.html(error_template);
                results.find("#message").html(result.error);
            }
        });
    });
}

function form_fetch_and_show_raw(name, endpoint, method, success_message) {
    const form = $(`#${name}-form`);
    const results = $(`#${name}-results`);
    form.submit(e => {
        e.preventDefault();
        results.empty();
        const params = form.serializeArray();

        var form_data = new FormData(form.closest("form")[0]);

        $.ajax({
            url: endpoint,
            method: method,
            credentials: "same-origin",
            headers: {
                Accept: "application/json"
            },
            data: form_data,
            processData: false,
            contentType: false
        }).done(result => {
           if (result.success) {
               results.html(success_template);
               results.find("#message").text(success_message);
           } else {
               results.html(error_template);
               results.find("#message").html(result.error);
           }
        }).fail(result => {
            results.html(error_template);
            if (result.status == 413) {
                results.find("#message").html("Failed to add this entry. Your file is too large.");
            } else {
                results.find("#message").html("Failed to add this entry: HTTP error code " + result.status + ". Please report this error.");
            }
        });
    });
}

$(() => {
    form_fetch_and_show("identity", `/dojo/${init.dojo}/course/identity`, "PATCH", "Your identity has been updated");
    form_fetch_and_show("add_ctf", `/dojo/${init.dojo}/course/add_ctf`, "PATCH", "A new approved CTF has been added");
    form_fetch_and_show_raw("ctfwriteup", "/submit_ctf_writeup", "POST", "A new CTF writeup has been submitted");

    let navLinks = document.querySelectorAll('.nav-link');
    navLinks.forEach(link => {
        link.addEventListener('click', function(e) {
            let pathSegments = window.location.pathname.split('/');
            let clickedSegment = this.id.split('-')[1];
            let courseIndexFromEnd = [...pathSegments].reverse().findIndex(seg => seg === 'course');
            let coursePosition = pathSegments.length - courseIndexFromEnd - 1;
            let newUrl = [...pathSegments.slice(0, coursePosition + 1), clickedSegment].join('/');
            history.pushState({}, '', newUrl);
        });
    });

    let pathSegments = window.location.pathname.split('/');
    let lastSegment = pathSegments[pathSegments.length - 1];
    let secondLastSegment = pathSegments[pathSegments.length - 2];

    if (lastSegment === 'course') return;

    if (secondLastSegment === 'course' && lastSegment) {
        let targetTab = document.querySelector(`#${lastSegment}`);
        let targetNav = document.querySelector(`#course-${lastSegment}-tab`);

        if (targetTab) {
            let activeTabs = document.querySelectorAll('.tab-pane.active');
            activeTabs.forEach(tab => {
                tab.classList.remove('active');
            });
            targetTab.classList.add('active');
            targetTab.classList.add('show');

            let activeNavs = document.querySelectorAll('.nav-link.active');
            activeNavs.forEach(nav => {
                nav.classList.remove('active');
            });
            targetNav.classList.add('active');
        }
    }
});
