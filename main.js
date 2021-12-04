var isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) && !window.MSStream;
if (true) {
    var video = document.getElementById('videoElement');
    var videoSrc = 'https://stream.moafunk.de/live/stream-io/index.m3u8';
    if (Hls.isSupported()) {
        var hls = new Hls();
        hls.loadSource(videoSrc);
        hls.attachMedia(video);
    }

    function play() {
        let btn = document.getElementById('btn-play');
        let playing = btn.className.includes("btn-pause");
        if (playing) {
            video.pause();
            btn.className = "btn";
        } else {
            video.play();
            btn.className = "btn btn-pause";
        }
    }
} else {
    if (flvjs.isSupported()) {
        var videoElement = document.getElementById('videoElement');
        var flvPlayer = flvjs.createPlayer({
                                           type: 'flv',
                                           url: 'https://stream.moafunk.de/live/stream-io.flv'
                                           });
        flvPlayer.attachMediaElement(videoElement);
        flvPlayer.load();

        function play() {
            let btn = document.getElementById('btn-play');
            let playing = btn.className.includes("btn-pause");
            if (playing) {
                flvPlayer.pause();
                btn.className = "btn";
            } else {
                flvPlayer.play();
                btn.className = "btn btn-pause";
            }
        }
    }

}
