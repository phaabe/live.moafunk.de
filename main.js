let video = document.getElementById('videoElement');

if (flvjs.isSupported()) {
    let flvPlayer = flvjs.createPlayer({
        type: 'flv',
        url: 'https://stream.moafunk.de/live/stream-io.flv'
    });
    flvPlayer.attachMediaElement(videoElement);
    flvPlayer.load();
} else if (Hls.isSupported()) {
    let videoSrc = 'https://stream.moafunk.de/live/stream-io/index.m3u8';
    let hls = new Hls();
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
