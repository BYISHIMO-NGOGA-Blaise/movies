document.addEventListener("DOMContentLoaded", function () {
    var uploadForm = document.querySelector("#upload-form");
    if (uploadForm) {
        uploadForm.addEventListener("submit", function (event) {
            event.preventDefault();
            var button = uploadForm.querySelector("button[type='submit']");
            var fileInput = uploadForm.querySelector('input[type="file"]');
            if (button && fileInput && fileInput.files.length) {
                button.disabled = true;
                button.textContent = "Uploading...";

                var progress = document.getElementById("upload-progress");
                var progressBar = document.getElementById("upload-progress-bar");
                var progressTrack = progress.querySelector(".upload-progress-track");
                var progressLabel = document.getElementById("upload-progress-label");
                progress.hidden = false;

                var request = new XMLHttpRequest();
                request.open("POST", uploadForm.action);
                request.upload.addEventListener("progress", function (event) {
                    if (!event.lengthComputable) {
                        return;
                    }
                    var percent = Math.round((event.loaded / event.total) * 100);
                    progressBar.style.width = percent + "%";
                    progressTrack.setAttribute("aria-valuenow", percent);
                    progressLabel.textContent = "Iri kugenda: " + percent + "%";
                });
                request.upload.addEventListener("load", function () {
                    progressLabel.textContent = "Upload yagenze. File irimo gutunganywa...";
                });
                request.addEventListener("load", function () {
                    if (request.status >= 200 && request.status < 400) {
                        window.location.href = request.responseURL || uploadForm.action.replace(/\/upload$/, "/");
                        return;
                    }
                    button.disabled = false;
                    button.textContent = "Hishe Filime";
                    progressLabel.textContent = "Upload yananiye (HTTP " + request.status + "). Ongera ugerageze.";
                });
                request.addEventListener("error", function () {
                    button.disabled = false;
                    button.textContent = "Hishe Filime";
                    progressLabel.textContent = "Ntacyo byakoreye kubera ihuriro. Ongera ugerageze.";
                });
                request.send(new FormData(uploadForm));
            }
        });
    }

    document.querySelectorAll(".flash").forEach(function (el) {
        setTimeout(function () {
            el.style.transition = "opacity .5s";
            el.style.opacity = "0";
            setTimeout(function () { el.remove(); }, 500);
        }, 5000);
    });
});
