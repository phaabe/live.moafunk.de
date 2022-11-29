let video = document.getElementById('videoElement');

if (flvjs.isSupported()) {
    let flvPlayer = flvjs.createPlayer({
        type: 'flv',
        url: 'https://stream.moafunk.de/live/stream-io.flv'
    });
    flvPlayer.attachMediaElement(videoElement);
    flvPlayer.load();
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
