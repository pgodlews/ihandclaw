import asyncio
from io import BytesIO
import json
import os
from pathlib import Path
import secrets
import sys
import time
import types

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from PIL import Image
import pytest
from fastapi.testclient import TestClient

from iphone_hand.airplay import AirPlayScreen, FrameUnavailable, UxPlaySupervisor
from iphone_hand.daemon import Config, create_app, make_link, runtime_frames
from iphone_hand.daemon_control import Paused
from iphone_hand.daemon_http import mjpeg_frames
from iphone_hand.elements import Elements, OCRUnavailable, TextBox
from iphone_hand.serial_link import ProtocolError, SerialLink
from test_tcp_link import link_for, server as tcp_server, tcp_clock

AIRPLAY = Path(__file__).resolve().parents[2]/'fixtures/airplay'


class FakeOCR:
    def __init__(self, boxes=()): self.boxes=list(boxes)
    def read(self, image): return self.boxes


@pytest.fixture
def app_rig(rig):
    phone, device, screen=rig
    reader=FakeOCR([TextBox('Settings',(100,100,200,150))])
    config=Config(token=secrets.token_urlsafe(32))
    app=create_app(config,phone=phone,screen=screen,elements=Elements(reader))
    with TestClient(app,base_url='http://iphone-hand.local') as client:
        yield app,client,device,screen,reader,config


def auth(config):return {'Authorization':'Bearer '+config.token}


@pytest.mark.parametrize('failure,expected', [
    (TimeoutError, 'timeout'), (ConnectionRefusedError, 'refused'),
    (ConnectionResetError, 'closed'), (BrokenPipeError, 'closed'),
    (lambda detail: ProtocolError('AUTH', detail), 'auth'),
    (lambda detail: ProtocolError(detail, detail), 'protocol'),
    (OSError, 'io'), (RuntimeError, 'unavailable'),
])
def test_state_error_is_allowlisted_without_exception_details(app_rig, failure, expected, monkeypatch):
    app, client, _, _, _, config = app_rig
    private = secrets.token_urlsafe(32)
    def unavailable(*args, **kwargs):
        raise failure(private)
    monkeypatch.setattr(app.state.controller.phone.link.link, 'command', unavailable)
    response = client.get('/api/state', headers=auth(config))
    assert response.status_code == 200
    assert response.json()['link'] == {'connected': False, 'error': expected}
    assert private not in response.text


@pytest.mark.parametrize('dropped', [False, True])
def test_state_recovers_after_reboot_and_connect_timeout(rig, tcp_server, tcp_clock, monkeypatch, dropped):
    import socket
    phone, _, screen = rig
    board = tcp_server()
    phone.link = link_for(board)
    config = Config(token=secrets.token_urlsafe(32))
    app = create_app(config, phone=phone, screen=screen, elements=Elements(FakeOCR()))
    with TestClient(app) as client:
        def state():
            started = tcp_clock.now
            response = client.get('/api/state', headers=auth(config))
            assert response.status_code == 200
            assert tcp_clock.now - started <= 2.001
            return response.json()
        assert state()['link'] == {'kind': 'tcp', 'connected': True}
        stale = phone.link.link.transport.sock
        if dropped:
            stale.close()
        else:
            monkeypatch.setattr(stale, 'send', lambda data: len(data))
            assert state()['link'] == {'connected': False, 'error': 'timeout'}
            assert stale.closed
            tcp_clock.sleep(2)
        connect = socket.create_connection
        attempts = []
        def connect_timeout(address, timeout):
            attempts.append(timeout)
            tcp_clock.sleep(timeout)
            raise TimeoutError(config.token)
        monkeypatch.setattr(socket, 'create_connection', connect_timeout)
        failed = state()
        assert failed['link'] == {'connected': False, 'error': 'timeout'}
        assert failed['ble'] == {'conn': False, 'ready': False}
        assert config.token not in json.dumps(failed)
        assert state()['link'] == failed['link']  # Cooldown answers immediately.
        assert len(attempts) == 1
        tcp_clock.sleep(2)
        monkeypatch.setattr(socket, 'create_connection', connect)
        recovered = state()
        assert recovered['link'] == {'kind': 'tcp', 'connected': True}
        assert recovered['ble'] == {'conn': True, 'ready': True}
        assert board.connections == 2


def test_state_http_deadline_shares_one_blocked_connect(rig, tcp_server, monkeypatch):
    import socket
    import threading
    phone, _, screen = rig
    board = tcp_server()
    phone.link = link_for(board)
    config = Config(token=secrets.token_urlsafe(32))
    app = create_app(config, phone=phone, screen=screen, elements=Elements(FakeOCR()))
    release = threading.Event()
    finished = threading.Event()
    attempts = []
    connect = socket.create_connection
    def blocked_connect(address, timeout):
        attempts.append(timeout)
        try:
            assert release.wait(8)
            return connect(address, timeout)
        finally:
            finished.set()
    monkeypatch.setattr(socket, 'create_connection', blocked_connect)
    async def exercise():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url=TestClient(app).base_url, headers=auth(config)) as client:
                started = time.monotonic()
                try:
                    responses = await asyncio.wait_for(asyncio.gather(
                        *(client.get('/api/state') for _ in range(3))), timeout=4)
                    assert 2.4 <= time.monotonic() - started < 4
                    assert len(attempts) == 1
                    for response in responses:
                        assert response.status_code == 200
                        assert response.json()['link'] == {'connected': False, 'error': 'timeout'}
                finally:
                    release.set()
                    assert await asyncio.to_thread(finished.wait, 2)
    asyncio.run(exercise())


def test_config_and_lazy_factory(tmp_path,monkeypatch):
    with pytest.raises(ValueError):Config(token='')
    with pytest.raises(ValueError):Config(token=' ',port=8790)
    with pytest.raises(ValueError):Config(token=secrets.token_hex(),port=True)
    p=tmp_path/'daemon.toml';p.write_text('port=8790\n')
    with pytest.raises(ValueError):Config.load(p)
    value=secrets.token_hex();p.write_text('token='+json.dumps(value)+'\n')
    assert Config.load(p).token==value
    assert value not in repr(Config.load(p))
    monkeypatch.delitem(sys.modules,'iphone_hand.tcp_link',raising=False)
    link=make_link('serial:/dev/ttyACM0')
    assert isinstance(link,SerialLink) and link.transport is None
    assert 'iphone_hand.tcp_link' not in sys.modules
    class Stub:
        def __init__(self,*args):self.args=args
    monkeypatch.setitem(sys.modules,'iphone_hand.tcp_link',types.SimpleNamespace(TcpLink=Stub))
    link=make_link('tcp://iphone-hand.local:7878',value)
    assert link.args==('iphone-hand.local',7878,value)
    for address in ('serial:relative','tcp://iphone-hand.local:7878','http://iphone-hand.local:7878',
                    'tcp://iphone-hand.local:7878/path'):
        with pytest.raises(ValueError):make_link(address)
    monkeypatch.delenv('XDG_RUNTIME_DIR',raising=False)
    with pytest.raises(ValueError):runtime_frames()
    monkeypatch.setenv('XDG_RUNTIME_DIR',str(tmp_path))
    assert runtime_frames()==tmp_path/'iphone-hand/frames'


def test_pointer_recovery_config_and_factory(tmp_path, monkeypatch, rig):
    phone, _, screen = rig
    value = secrets.token_urlsafe(32)
    path = tmp_path / 'daemon.toml'
    path.write_text('token=' + json.dumps(value) + '\npointer_recover_key="esc"\n')
    config = Config.load(path)
    assert config.pointer_recover_key == 'esc'
    assert Config(token=value).pointer_recover_key == ''
    assert Config(token=value, pointer_recover_key='').pointer_recover_key == ''
    for invalid in (None, True, 'shift', 'bad+esc'):
        with pytest.raises(ValueError, match='pointer_recover_key'):
            Config(token=value, pointer_recover_key=invalid)
    path.write_text(path.read_text() + 'unknown_key=true\n')
    with pytest.raises(TypeError):
        Config.load(path)
    supplied = {}

    def make_phone(link, passed_screen, **kwargs):
        supplied.update(kwargs)
        assert passed_screen is screen
        return phone

    monkeypatch.setattr('iphone_hand.daemon.make_link', lambda *args: phone.link)
    monkeypatch.setattr('iphone_hand.daemon.Phone', make_phone)
    create_app(config, screen=screen, elements=Elements(FakeOCR()))
    assert supplied['pointer_recover_key'] == 'esc'


@pytest.mark.parametrize('recover,returns', [(False, False), (True, True), (True, False)])
def test_pointer_hidden_http_and_action_log(app_rig, recover, returns):
    app, client, device, screen, _, config = app_rig
    phone = app.state.controller.phone
    phone.motion.pointer_recover_key = 'esc' if recover else ''
    screen.hidden = True
    original = phone.motion.link.command

    def command(name, *args, **kwargs):
        reply = original(name, *args, **kwargs)
        if name == 'KEY' and returns:
            screen.hidden = False
        return reply

    phone.motion.link.command = command
    response = client.post('/api/tap', json={'x': 100, 'y': 200}, headers=auth(config))
    data = response.json()
    entry = app.state.controller.log[-1]
    if returns:
        assert response.status_code == 200
        assert data['result']['recovered_with'] == 'esc'
        assert data['state']['action_log'][-1]['recovered_with'] == 'esc'
        assert entry['ok']
    else:
        assert response.status_code == 503
        assert data['error_class'] == 'PointerHidden'
        assert data['error'] == 'pointer hidden (text field editing?) - press esc to end editing'
        assert entry['error'] == 'PointerHidden'
        assert not entry['ok']
        assert not any(c[1] == 'CLICK' for c in device.commands)
    if recover:
        assert entry['recovered_with'] == 'esc'
        assert [c[2] for c in device.commands if c[1] == 'KEY'] == [('0', '41')]
    else:
        assert 'recovered_with' not in entry
        assert not any(c[1] == 'KEY' for c in device.commands)
    assert config.token not in json.dumps(data)


def test_recovery_logged_when_later_click_fails(app_rig):
    app, client, _, screen, _, config = app_rig
    phone = app.state.controller.phone
    phone.motion.pointer_recover_key = 'esc'
    screen.hidden = True
    original = phone.motion.link.command

    def command(name, *args, **kwargs):
        if name == 'CLICK':
            raise RuntimeError(config.token)
        reply = original(name, *args, **kwargs)
        if name == 'KEY':
            screen.hidden = False
        return reply

    phone.motion.link.command = command
    response = client.post('/api/tap', json={'x': 100, 'y': 200}, headers=auth(config))
    assert response.status_code == 503
    assert config.token not in response.text
    assert app.state.controller.log[-1]['recovered_with'] == 'esc'
    assert not app.state.controller.log[-1]['ok']


def write_frames(directory, now=None):
    directory.mkdir(exist_ok=True)
    now=time.time() if now is None else now
    fixture=(AIRPLAY/'home-voicecontrol-pointer.jpg').read_bytes()
    for n in range(3):
        p=directory/f'f{n:05d}.jpg';p.write_bytes(fixture)
        os.utime(p,(now-.1*(2-n),now-.1*(2-n)))


def test_airplay_safe_frame_staleness_and_newest(tmp_path):
    now=time.time();screen=AirPlayScreen(tmp_path,clock=lambda:now)
    with pytest.raises(FrameUnavailable):screen.grab()
    write_frames(tmp_path,now)
    newest=tmp_path/'f00002.jpg';newest.write_bytes(b'incomplete jpeg')
    assert screen.grab().size==(1126,2434)
    assert screen.grab(newest=True).size==(1126,2434)
    assert screen.rect.box==(0,0,1126,2434)
    assert screen.status()['mirroring'] and screen.status()['fps']>0
    screen.clock=lambda:now+3
    assert not screen.status()['mirroring'] and screen.status()['fps']==0
    with pytest.raises(FrameUnavailable):screen.grab()
    with pytest.raises(FrameUnavailable):screen.grab(newest=True)


def test_fake_uxplay_process_and_restart(tmp_path):
    script=tmp_path/'fake_receiver.py'
    script.write_text('''import pathlib,sys,time
frames=pathlib.Path(sys.argv[1]);fixture=pathlib.Path(sys.argv[2]).read_bytes()
for n in range(3):
 (frames/f'f{n:05d}.jpg').write_bytes(fixture)
 time.sleep(.01)
time.sleep(.06)
''')
    calls=[]
    async def factory(*command):
        calls.append(command)
        return await asyncio.create_subprocess_exec(sys.executable,str(script),str(tmp_path/'frames'),
                                                    str(AIRPLAY/'home-voicecontrol-pointer.jpg'))
    async def exercise():
        supervisor=UxPlaySupervisor(tmp_path/'frames',executable='uxplay',key_file=tmp_path/'k.pem',
                                    process_factory=factory,backoff_initial=.02,backoff_max=.04)
        await supervisor.start()
        screen=AirPlayScreen(tmp_path/'frames')
        deadline=time.monotonic()+3
        while not screen.status()['mirroring']:
            assert time.monotonic()<deadline
            await asyncio.sleep(.01)
        assert screen.grab().size==(1126,2434)
        while supervisor.restarts<2:
            assert time.monotonic()<deadline
            await asyncio.sleep(.01)
        await supervisor.stop()
        assert not supervisor.status()['running'] and supervisor.task is None
        assert len(calls)>=2
        command=calls[0]
        assert command[:10]==('uxplay','-n','iPhone Hand','-nh','-key',str(tmp_path/'k.pem'),'-s','1125x2436@60','-fps','60')
        assert 'max-files=5' in command[-1]
    asyncio.run(exercise())


def test_every_route_auth_first(app_rig):
    app,client,_,_,_,config=app_rig
    # Enumerate actual registered SDK and REST routes, including streaming and index.
    for route in app.routes:
        for method in route.methods or {'GET','POST','DELETE'}:
            response=client.request(method,route.path)
            assert response.status_code==401,(method,route.path,response.text)
            assert response.headers['www-authenticate']=='Bearer'
    for method in ('GET','POST','DELETE','OPTIONS'):
        assert client.request(method,'/mcp').status_code==401
        assert client.request(method,'/missing').status_code==401
    assert client.get('/api/state',headers={'Authorization':'Bearer '+secrets.token_hex()}).status_code==401
    assert client.get('/api/state',params={'token':config.token}).status_code==200
    assert client.get('/',headers=auth(config)).status_code==200
    assert client.get('/api/state',headers={**auth(config),'Origin':'http://iphone-hand.local'}).status_code==200
    assert client.get('/api/state',headers={**auth(config),'Origin':'null'}).status_code==403


@pytest.mark.parametrize('route,body,report',[
 ('move',{'x':100,'y':150},'MOVE'),('tap',{'x':100,'y':150},'BTN'),
 ('long_press',{'x':100,'y':150,'ms':300},'WAIT'),
 ('drag',{'x1':100,'y1':150,'x2':130,'y2':180,'steps':2},'BTN'),
 ('scroll',{'dy':2,'dx':-1},'SCROLL'),('type',{'text':'Hi'},'KEY'),
 ('key',{'combo':'cmd+space'},'KEY'),('home',{},'KEY'),
 ('back',{},'BTN'),('launch',{'app':'Settings'},'KEY'),
 ('tap_text',{'text':'Settings'},'BTN'),('tap_element',{'id':'t0'},'BTN'),
 ('wait_for',{'text':'Settings','timeout':0},None),
 ('scroll_until',{'text':'Settings','max_scrolls':0},None),
 ('run_shortcut',{'name':'Settings'},'KEY'),
])
def test_action_routes_fake_serial(app_rig,route,body,report):
    app,client,device,_,_,config=app_rig
    response=client.post('/api/'+route,json=body,headers=auth(config))
    assert response.status_code==200,response.text
    data=response.json()
    assert 'result' in data and 'state' in data
    assert data['state']['action_log'][-1]['action']==route
    assert data['state']['action_log'][-1]['ok']
    assert data['state']['ble']['ready']
    if report:assert any(r[0]==report for r in device.reports)
    assert device.buttons==0


def test_pause_blocks_all_action_routes(app_rig):
    app,client,device,_,_,config=app_rig
    assert client.post('/api/pause',json={'paused':True},headers=auth(config)).status_code==200
    before=list(device.reports)
    for action in app.state.controller.ACTIONS:
        assert client.post('/api/'+action,json={},headers=auth(config)).status_code==423
    assert device.reports==before
    assert client.get('/api/state',headers=auth(config)).json()['paused']
    assert client.get('/api/frame.jpg',headers=auth(config)).status_code==200
    assert client.post('/api/pause',json={'paused':'false'},headers=auth(config)).status_code==422
    assert client.post('/api/pause',json={'paused':False},headers=auth(config)).status_code==200
    assert client.post('/api/home',json={},headers=auth(config)).status_code==200


@pytest.mark.parametrize('text',['Password','Passcode','Hasło','Kod'])
def test_typing_guard_no_key_reports(app_rig,text):
    _,client,device,_,reader,config=app_rig
    reader.boxes=[TextBox(text,(10,10,120,40))]
    response=client.post('/api/type',json={'text':secrets.token_hex()},headers=auth(config))
    assert response.status_code==409
    assert not any(r[0]=='KEY' for r in device.reports)
    assert response.json()['error'].startswith('Typing refused')


def test_typing_fails_closed_without_ocr_and_log_redaction(app_rig):
    app,client,device,_,reader,config=app_rig
    class Missing:
        def read(self,image):raise OCRUnavailable('missing')
    app.state.controller.detector.reader=Missing()
    secret=secrets.token_urlsafe(20)
    assert client.post('/api/type',json={'text':secret},headers=auth(config)).status_code==409
    data=client.get('/api/state',headers=auth(config)).text
    assert secret not in data and config.token not in data
    assert not device.reports


def test_elements_frame_and_annotate_routes_airplay(tmp_path,rig):
    phone,_,_=rig
    write_frames(tmp_path)
    screen=AirPlayScreen(tmp_path)
    config=Config(token=secrets.token_urlsafe(32))
    app=create_app(config,phone=phone,screen=screen,elements=Elements(FakeOCR()))
    with TestClient(app,base_url='http://iphone-hand.local') as client:
        result=client.get('/api/elements',headers=auth(config))
        assert result.status_code==200
        assert len(result.json())>=20
        assert abs(json.loads(result.headers['x-pointer-pt'])['x']-211.6)<1
        for path,size in [('/api/frame.jpg',(1126,2434)),('/api/frame.jpg?scale=pt2',(750,1624)),
                          ('/api/elements?annotate=1',(1126,2434))]:
            response=client.get(path,headers=auth(config))
            assert response.status_code==200 and response.headers['content-type']=='image/jpeg'
            assert response.headers['cache-control']=='no-store'
            assert Image.open(BytesIO(response.content)).size==size
        for p in tmp_path.glob('*.jpg'):os.utime(p,(0,0))
        assert client.get('/api/frame.jpg',headers=auth(config)).status_code==503
        assert client.get('/api/elements',headers=auth(config)).status_code==503
        assert client.post('/api/home',json={},headers=auth(config)).status_code==503


def test_mjpeg_framing_and_rate(rig):
    _,_,screen=rig
    async def exercise():
        sleeps=[];count=0
        async def disconnected():return count>=2
        async def sleeper(value):sleeps.append(value)
        async for part in mjpeg_frames(screen,disconnected,sleeper=sleeper):
            count+=1
            headers,payload=part.split(b'\r\n\r\n',1)
            assert headers.startswith(b'--frame\r\nContent-Type: image/jpeg')
            assert payload.endswith(b'\r\n')
            length=int(headers.split(b'Content-Length: ')[1])
            assert length==len(payload)-2
            assert Image.open(BytesIO(payload[:-2])).size==(750,1624)
        assert count==2 and all(s>=.1 for s in sleeps)
    asyncio.run(exercise())


def test_official_mcp_http_round_trip(rig):
    phone,device,screen=rig
    config=Config(token=secrets.token_urlsafe(32))
    app=create_app(config,phone=phone,screen=screen,elements=Elements(FakeOCR([TextBox('Settings',(10,10,150,50))])))
    async def exercise():
        async with app.router.lifespan_context(app):
            transport=httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport,headers=auth(config)) as client:
                async with streamable_http_client('http://iphone-hand.local/mcp',http_client=client) as (read,write,_):
                    async with ClientSession(read,write) as session:
                        await session.initialize()
                        names={t.name for t in (await session.list_tools()).tools}
                        assert {'screenshot','tap','type_text','elements','tap_element','tap_text','launch','back',
                                'wait_for','scroll_until','run_shortcut'} <= names
                        assert not (await session.call_tool('screenshot',{'mark_pointer':False})).isError
                        assert not (await session.call_tool('home',{})).isError
                        app.state.controller.set_paused(True)
                        reports=list(device.reports)
                        assert (await session.call_tool('home',{})).isError
                        assert device.reports==reports
                        assert not (await session.call_tool('elements',{})).isError
                        app.state.controller.set_paused(False)
                        app.state.controller.detector.reader=FakeOCR([TextBox('Password',(0,0,100,40))])
                        reports=list(device.reports)
                        assert (await session.call_tool('type_text',{'text':'unsafe'})).isError
                        assert device.reports==reports
    asyncio.run(exercise())


def test_pause_interrupts_drag_and_releases_button(app_rig,monkeypatch):
    app,_,device,screen,_,_=app_rig
    controller=app.state.controller
    original=screen.grab
    paused=False
    def capture():
        nonlocal paused
        image=original()
        if device.buttons and not paused:
            paused=True
            controller.set_paused(True)
            # Even an immediate resume must not revive the interrupted drag.
            controller.set_paused(False)
        return image
    monkeypatch.setattr(screen,'grab',capture)
    with pytest.raises(Paused):
        controller.action('drag',x1=100,y1=150,x2=250,y2=500,steps=4)
    assert device.buttons==0
    assert device.reports[-1]==('BTN',0)
    assert not controller.log[-1]['ok']


def test_pause_http_responds_during_blocked_mcp_action(rig):
    import threading
    phone,device,screen=rig
    config=Config(token=secrets.token_urlsafe(32))
    entered=threading.Event();release=threading.Event()
    class BlockingOCR:
        def read(self,image):
            entered.set()
            assert release.wait(3)
            return []
    app=create_app(config,phone=phone,screen=screen,elements=Elements(BlockingOCR()))
    async def exercise():
        async with app.router.lifespan_context(app):
            transport=httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport,base_url='http://iphone-hand.local',headers=auth(config)) as client:
                action=asyncio.create_task(app.state.mcp.call_tool('type_text',{'text':'Hi'}))
                assert await asyncio.to_thread(entered.wait,2)
                try:
                    response=await asyncio.wait_for(client.post('/api/pause',json={'paused':True}),timeout=1)
                    assert response.status_code==200 and response.json()['paused']
                finally:
                    release.set()
                with pytest.raises(Exception,match='paused'):
                    await action
                assert not any(r[0]=='KEY' for r in device.reports)
    asyncio.run(exercise())


def test_wait_and_scroll_bounds(app_rig):
    _,client,device,_,reader,config=app_rig
    reader.boxes=[]
    assert client.post('/api/wait_for',json={'text':'Missing','timeout':0},headers=auth(config)).status_code==504
    assert client.post('/api/scroll_until',json={'text':'Missing','timeout':1,'max_scrolls':1},headers=auth(config)).status_code==504
    assert len([r for r in device.reports if r[0]=='SCROLL'])==1
    for route,body in [('tap',{'x':-1,'y':2}),('scroll',{'dy':True}),
                        ('launch',{}),('wait_for',{'text':'Missing','timeout':61})]:
        assert client.post('/api/'+route,json=body,headers=auth(config)).status_code==422


def test_supervisor_spawn_error_backoff_and_shutdown_race(tmp_path):
    async def failure():
        calls=[]
        async def factory(*args):
            calls.append(time.monotonic())
            raise FileNotFoundError
        supervisor=UxPlaySupervisor(tmp_path/'missing',process_factory=factory,backoff_initial=.02,backoff_max=.08)
        await supervisor.start()
        deadline=time.monotonic()+3
        while len(calls)<3:
            assert time.monotonic()<deadline
            await asyncio.sleep(.005)
        await supervisor.stop()
        assert calls[1]-calls[0]>=.018 and calls[2]-calls[1]>=.038
        assert supervisor.error and supervisor.task is None
    async def race():
        entered=asyncio.Event();go=asyncio.Event()
        class Process:
            returncode=None
            def terminate(self):self.returncode=-15
            async def wait(self):
                while self.returncode is None:await asyncio.sleep(.01)
                return self.returncode
        process=Process()
        async def factory(*args):
            entered.set();await go.wait();return process
        supervisor=UxPlaySupervisor(tmp_path/'race',process_factory=factory)
        await supervisor.start();await entered.wait()
        stop=asyncio.create_task(supervisor.stop());await asyncio.sleep(0);go.set()
        await asyncio.wait_for(stop,1)
        assert process.returncode==-15 and supervisor.task is None
    asyncio.run(failure());asyncio.run(race())


def test_receiver_allow_option_and_state_without_link(tmp_path,rig):
    phone,device,screen=rig
    allowed=secrets.token_hex()
    receiver=UxPlaySupervisor(tmp_path,allow_device_id=allowed)
    assert receiver.command()[-3:]==['-restrict','-allow',allowed]
    config=Config(token=secrets.token_urlsafe())
    app=create_app(config,phone=phone,screen=screen,elements=Elements(FakeOCR()))
    def unavailable(*args,**kwargs):raise ConnectionError
    phone.link.link.command=unavailable
    with TestClient(app,base_url='http://iphone-hand.local') as client:
        state=client.get('/api/state',headers=auth(config)).json()
        assert not state['link']['connected'] and not state['ble']['ready']
        assert allowed not in json.dumps(state)


def test_static_screen_counts_as_mirroring_while_client_connected(tmp_path):
    # iOS sends no frames while the screen is still; an old frame stays valid while connected.
    import os
    from iphone_hand.airplay import AirPlayScreen
    source = (AIRPLAY/'home-voicecontrol-pointer.jpg').read_bytes()
    for i in range(2):
        path = tmp_path/f'f{i:05d}.jpg'
        path.write_bytes(source)
        os.utime(path, (1000+i, 1000+i))
    now = 1000+600
    connected = AirPlayScreen(tmp_path, clock=lambda: now, connected=lambda: True)
    assert connected.status()['mirroring'] and connected.grab().size == (1126, 2434)
    gone = AirPlayScreen(tmp_path, clock=lambda: now, connected=lambda: False)
    assert not gone.status()['mirroring']
    with pytest.raises(Exception):
        gone.grab()


def test_type_route_counts_options_redaction(app_rig, monkeypatch):
    app, client, device, _, _, config = app_rig
    text = 'private typing test'
    response = client.post('/api/type', json={'text': text, 'window': 2, 'hold_ms': 5}, headers=auth(config))
    assert response.status_code == 200 and response.json()['result']['typed'] == len(text)
    assert all(c[2][-1] == '5' for c in device.commands if c[1] == 'KEY')
    original = device._execute
    baseline = len(device.commands)
    def busy(command, args):
        return 'ERR BUSY' if len(device.commands) == baseline + 3 else original(command, args)
    monkeypatch.setattr(device, '_execute', busy)
    response = client.post('/api/type', json={'text': text}, headers=auth(config))
    assert response.status_code == 503
    assert response.json()['typed'] == 7 and response.json()['sent'] == 8
    assert response.json()['confirmed_indices'] == [0, 1, 3, 4, 5, 6, 7]
    assert text not in response.text and text not in str(app.state.controller.cached_state()['action_log'])


def test_pause_during_type_stops_submissions_and_drains(app_rig, monkeypatch):
    app, client, device, _, _, config = app_rig
    write = device.write
    def pause_after_two(data):
        result = write(data)
        if len(device.commands) == 2: app.state.controller.set_paused(True)
        return result
    monkeypatch.setattr(device, 'write', pause_after_two)
    response = client.post('/api/type', json={'text': 'longer than window'}, headers=auth(config))
    assert response.status_code == 423
    assert response.json()['typed'] == response.json()['sent'] == 2
    assert len(device.commands) == 2


@pytest.mark.parametrize('body', [{'text': 'valid prefix☃'}, {'text': 'ok', 'window': 13}, {'text': 'ok', 'hold_ms': -1}])
def test_type_validation_errors_report_zero_before_any_key(app_rig, body):
    _, client, device, _, _, config = app_rig
    response = client.post('/api/type', json=body, headers=auth(config))
    assert response.status_code == 422 and response.json()['typed'] == 0
    assert not any(c[1] == 'KEY' for c in device.commands)
