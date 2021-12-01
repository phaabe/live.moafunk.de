if (flvjs.isSupported()) {
    var videoElement = document.getElementById('videoElement');
    var flvPlayer = flvjs.createPlayer({
        type: 'flv',
        url: 'https://stream.moafunk.de/live/stream-io.flv'
    });
    flvPlayer.attachMediaElement(videoElement);
    flvPlayer.load();

    function play() {
        flvPlayer.play();
    }

    function pause() {
        flvPlayer.pause();
    }
}