document.addEventListener("DOMContentLoaded", function () {
    var uploadForm = document.querySelector("form[action$='/upload']");
    if (uploadForm) {
        uploadForm.addEventListener("submit", function () {
            var button = uploadForm.querySelector("button[type='submit']");
            var fileInput = uploadForm.querySelector('input[type="file"]');
            if (button && fileInput && fileInput.files.length) {
                button.disabled = true;
                button.textContent = "Uploading...";
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
