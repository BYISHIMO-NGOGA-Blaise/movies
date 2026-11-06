document.addEventListener("DOMContentLoaded", function () {
    var video = document.getElementById("live-video");
    if (!video) {
        return;
    }

    var streamUrl = video.dataset.streamUrl;
    if (window.Hls && window.Hls.isSupported()) {
        var hls = new window.Hls({
            enableWorker: true,
            lowLatencyMode: true
        });
        hls.loadSource(streamUrl);
        hls.attachMedia(video);
    } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
        video.src = streamUrl;
    }
});
