
let video;
let btn;
let playing;
let live;

let platform = navigator?.userAgentData?.platform || navigator?.platform || 'unknown'

fetch('https://stream.moafunk.de/live/stream-io/index.m3u8', { method: 'HEAD' })
  .then(response => {
    if (response.status === 200) {
      document.querySelector('#status').innerHTML = 'Live now';
      live=true;
    } else {
      document.querySelector('#status').innerHTML = 'Off air<br/><span style="font-size:13pt;">(we announce shows via Tele- and Instagram)</span>';
      live=false;
    }
  })
  .catch(error => {
    console.error('Error:', error);
    document.querySelector('#status').innerHTML = 'Off';
    live=false;
  });

if (/iPhone|iPod|iPad/.test(platform)){
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
      if (live){
        video.play();
        btn.className = "btn btn-pause";
      } else {
        // btn.className = "btn btn-off";
      }
    }
}

