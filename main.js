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