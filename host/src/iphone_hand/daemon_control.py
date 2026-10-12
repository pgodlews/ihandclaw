"""Shared HTTP/MCP guards, serialized skills, and bounded in-memory action log."""
from collections import deque
from difflib import SequenceMatcher
import math
import threading
import time

from .airplay import FrameUnavailable
from .elements import password_prompt
from .geometry import default_geometry
from .keys import text_keys
from .phone import integer
from .link_errors import link_error_class
from .pointer import find_pointer
from .screen import PhoneRect


class Paused(RuntimeError):
    pass


class PasswordPrompt(RuntimeError):
    pass


class GuardedLink:
    """Recheck pause before each report, including within multi-command Phone actions."""
    def __init__(self, link, check):
        self.link, self.check = link, check

    def __getattr__(self, name):
        return getattr(self.link, name)

    def pipeline(self, commands, **kwargs):
        # Check pause/generation on each refill; already submitted commands drain.
        return self.link.pipeline(commands, before_send=self.check, **kwargs)

    def command(self, command, *args, **kwargs):
        release = command == 'BTN' and args == (0,) or command == 'KREPORT' and args and all(a == 0 for a in args)
        if command not in ('STATUS', 'PING') and not release:
            self.check()
        return self.link.command(command, *args, **kwargs)


class Controller:
    ACTIONS = {'tap', 'long_press', 'drag', 'scroll', 'move', 'type', 'key', 'home', 'back',
               'launch', 'tap_text', 'tap_element', 'scroll_until', 'wait_for', 'run_shortcut'}

    def __init__(self, phone, screen, elements, receiver=None):
        self.phone, self.screen, self.detector, self.receiver = phone, screen, elements, receiver
        self.paused = threading.Event()
        self.lock = threading.RLock()
        self.generation = 0
        self.active_generation = threading.local()
        self.log_lock = threading.Lock()
        self.log = deque(maxlen=100)
        self._state = {}
        if hasattr(phone, 'link'):
            phone.link = GuardedLink(phone.link, self.check)
            phone.motion.link = phone.link

    def check(self):
        if self.paused.is_set() or getattr(self.active_generation, 'value', self.generation) != self.generation:
            raise Paused('Actions are paused')

    def set_paused(self, value):
        if not isinstance(value, bool):
            raise ValueError('paused must be a boolean')
        if value:
            self.generation += 1
        self.paused.set() if value else self.paused.clear()
        return self.cached_state()

    def geometry(self):
        get = getattr(self.phone, 'geometry', None)
        return get() if get else default_geometry()

    def rect(self, frame):
        """Screen rect and point size of a frame: the Phone's resolver is the one source."""
        resolve = getattr(self.phone, 'phone_rect', None)
        if resolve is not None:
            return resolve(frame)
        return PhoneRect(0, 0, *frame.size, *self.geometry().pt)

    def public_size(self, frame):
        return self.rect(frame).geometry.public_size

    def cached_state(self):
        with self.log_lock:
            return {**self._state, 'paused': self.paused.is_set(), 'action_log': list(self.log)}

    def state(self):
        with self.lock:
            airplay = self.screen.status()
            airplay['receiver_name'] = getattr(self, 'receiver_name', 'iPhone Hand')
            airplay['frame_source'] = getattr(self, 'frame_source', 'airplay')
            position = None
            screen = self.geometry()
            try:
                frame = self.screen.grab()
                rect = self.rect(frame)
                screen = rect.geometry
                position = find_pointer(frame, rect, getattr(self.phone, 'pointer_detector', None))
            except FrameUnavailable:
                pass
            try:
                if hasattr(self.phone, 'link'):
                    reply = self.phone.link.command('STATUS', timeout=2.0).fields
                    hardware = {'ble': {'conn': reply.get('conn') == '1', 'ready': reply.get('ready') == '1'}}
                    link = {'kind': 'tcp' if type(self.phone.link.link).__name__ == 'TcpLink' else 'serial',
                            'connected': True}
                else:
                    hardware = self.phone.status(); link = {'kind': 'fake', 'connected': True}
            except Exception as error:
                hardware = {'ble': {'conn': False, 'ready': False}}
                link = {'connected': False, 'error': link_error_class(error)}
            self._state = {**hardware, 'link': link, 'airplay': airplay, 'screen': screen.to_dict(),
                           'pointer': dict(zip(('x', 'y', 'confidence'), position)) if position else None,
                           'receiver': self.receiver.status() if self.receiver else None}
            return self.cached_state()

    def elements(self):
        return self.analyze(self.screen.grab())

    def analyze(self, frame):
        return self.detector.analyze(frame, self.rect(frame))

    def _type(self, text, *, window=8, hold_ms=None):
        if not isinstance(text, str) or len(text) > 4096:
            raise ValueError('text must be a string of at most 4096 characters')
        result = self.elements()
        if not result['ocr_available']:
            raise PasswordPrompt('Typing requires working OCR for the password guard')
        if password_prompt(result):
            raise PasswordPrompt('Password/passcode prompt detected; typing refused')
        self.check()
        return self.phone.type_text(text, window=window, hold_ms=hold_ms)

    def match(self, text, nth=0):
        if not isinstance(text, str) or not text.strip():
            raise ValueError('text must be nonempty')
        integer(nth, 'nth', 0, 999)
        text = text.casefold().strip()
        matches = []
        for item in self.elements()['elements']:
            label = item['text'].casefold()
            score = 1 if label == text else .9 if text in label else SequenceMatcher(None, text, label).ratio()
            if score >= .65:
                matches.append((score, item))
        matches.sort(key=lambda p: (-p[0], p[1]['bounds_pt'][1], p[1]['bounds_pt'][0]))
        if nth >= len(matches):
            raise LookupError('Text not found')
        return matches[nth][1]

    def _wait(self, text, timeout=10.0, *, scroll=False, dy=-3, max_scrolls=10):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 <= timeout <= 60:
            raise ValueError('timeout must be in 0..60 seconds')
        integer(max_scrolls, 'max_scrolls', 0, 100)
        integer(dy, 'dy', -127, 127)
        deadline = time.monotonic()+timeout
        count = 0
        while True:
            self.check()
            try:
                return self.match(text)
            except LookupError:
                if time.monotonic() >= deadline or scroll and count >= max_scrolls:
                    raise TimeoutError('Text did not appear within the configured bound')
            if scroll:
                self.check()
                self.phone.scroll(dy); count += 1
            # A pause wakes a waiting skill immediately.
            self.paused.wait(min(.35, max(0, deadline-time.monotonic())))

    def _execute(self, action, args):
        if action in ('tap', 'long_press', 'drag', 'scroll', 'key', 'home'):
            return getattr(self.phone, action)(**args)
        if action == 'move':
            return self.phone.move_to(**args)
        if action == 'type':
            return self._type(**args)
        if action == 'back':
            if args:
                raise ValueError('back takes no arguments')
            # Left-edge swipe at mid height to 75 % of the width (280, 406 on the 375x812 reference).
            width, height = self.geometry().pt
            return self.phone.drag(3, height / 2, round(width * 280 / 375), height / 2, steps=10)
        if action in ('launch', 'run_shortcut'):
            field = 'app' if action == 'launch' else 'name'
            name = args.get(field)
            if set(args) != {field} or not isinstance(name, str) or not name.strip() or len(name) > 200:
                raise ValueError('app/shortcut must be a nonempty name of at most 200 characters')
            text_keys(name)  # Reject untypeable names before Spotlight is opened.
            self.phone.key('cmd+space')
            self.paused.wait(.35)
            self.check()
            self._type(name)
            self.check()
            return self.phone.key('enter')
        if action == 'tap_text':
            item = self.match(**args)
            self.check()
            return self.phone.tap(*item['center'])
        if action == 'tap_element':
            if set(args) != {'id'}:
                raise ValueError('tap_element requires id')
            item = next((i for i in self.elements()['elements'] if i['id'] == args['id']), None)
            if item is None:
                raise LookupError('Element not found in current frame')
            self.check()
            return self.phone.tap(*item['center'])
        if action in ('wait_for', 'scroll_until'):
            return self._wait(**args, scroll=action == 'scroll_until')
        raise ValueError('Unknown action')

    def action(self, action, **args):
        generation = self.generation
        with self.lock:
            self.active_generation.value = generation
            started = time.perf_counter()
            entry = {'action': action, 'time': time.time()}
            motion = getattr(self.phone, 'motion', None)
            recovery_count = getattr(motion, 'recovery_count', 0)
            # Typed strings, app names, combos, and exceptions may contain secrets. Do not log them.
            try:
                self.check()
                self.rect(self.screen.grab())  # Fail closed on a stale mirror; refresh geometry.
                result = self._execute(action, args)
                entry['ok'] = True
                if isinstance(result, dict) and result.get('recovered_with'):
                    entry['recovered_with'] = result['recovered_with']
            except Exception as exc:
                entry['ok'] = False
                entry['error'] = type(exc).__name__
                raise
            finally:
                # Preserve the side effect even if a later click/capture fails.
                if getattr(motion, 'recovery_count', 0) != recovery_count:
                    entry['recovered_with'] = motion.last_recover_key
                del self.active_generation.value
                entry['duration_ms'] = (time.perf_counter()-started)*1000
                with self.log_lock:
                    self.log.append(entry)
            return {'result': result, 'state': self.state()}

    # Proxy with the existing Phone API: all SDK tools use these same guards/logging.
    def move_to(self, x, y): return self.action('move', x=x, y=y)['result']
    def tap(self, x, y, button='left'): return self.action('tap', x=x, y=y, button=button)['result']
    def long_press(self, x, y, ms=800): return self.action('long_press', x=x, y=y, ms=ms)['result']
    def drag(self, x1, y1, x2, y2, steps=10): return self.action('drag', x1=x1, y1=y1, x2=x2, y2=y2, steps=steps)['result']
    def scroll(self, dy, dx=0, x=None, y=None): return self.action('scroll', dy=dy, dx=dx, x=x, y=y)['result']
    def type_text(self, text): return self.action('type', text=text)['result']
    def key(self, combo): return self.action('key', combo=combo)['result']
    def home(self): return self.action('home')['result']
    def status(self): return self.state()
    def screenshot_with_pointer(self):
        with self.lock:
            return self.phone.screenshot_with_pointer()
    def screenshot(self):
        with self.lock:
            return self.phone.screenshot()
    def find_pointer(self):
        with self.lock:
            return self.phone.find_pointer()
