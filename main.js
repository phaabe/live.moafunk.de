
let video;
let btn;
let playing;


if (/iPhone|iPod|iPad/.test(navigator.platform)){
//if (true){
    console.log('is iOS');
    video = document.getElementById('player');
}else if(flvjs.isSupported()){
    console.log('flvjs is supported, this is not iOS')
    video = document.getElementById('videoElement');
    console.log(video);

    let flvPlayer = flvjs.createPlayer({
        type: 'flv',
        url: 'https://stream.moafunk.de/live/stream-io.flv'
    });
    flvPlayer.attachMediaElement(videoElement);
    flvPlayer.load();
}else{
    console.log(avigator.platform + ' not supported as platform for streaming!')
}

function play() {
    btn = document.getElementById('btn-play');
    playing = btn.className.includes("btn-pause");
    if (playing) {
        video.pause();
        btn.className = "btn";
    } else {
        video.play();
        btn.className = "btn btn-pause";
    }
}