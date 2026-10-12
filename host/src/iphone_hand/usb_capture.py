"""macOS-only QuickTime-style USB capture of the iPhone screen (AVFoundation via PyObjC).

Frames stay in memory; nothing is written to disk. All PyObjC imports are lazy (inside the
backend) so Linux and airplay-mode deployments never touch AVFoundation, and tests inject a
fake backend. Read (deploy/macos/README.md): permission, trust and concurrency notes.
"""
from collections import deque
import threading
import time

from . import geometry
from .airplay import FrameUnavailable
from .screen import PhoneRect


class UsbCaptureUnavailable(RuntimeError):
    pass


# Measured (over AirPlay, 2026-10-12): iPhone XS frames 1126x2434 for 375x812 pt.
XS_PX_PER_PT = 1126 / 375  # 3.0027


def _pt_size(size, scale):
    """Point size for a captured frame: configured per-model scale, else geometry.detect.

    Estimated (untested on hardware): USB capture delivers native pixels, so a frame matches
    one geometry.IPHONE_SIZES row at its integer scale; unknown shapes fall back to @3x.
    Self-consistent either way: PhoneRect carries the point size for every px<->pt conversion.
    """
    return geometry.detect(size, px_per_pt=scale, native=True).pt


def rgba_image(data, width, height, bytes_per_row):
    """RGB image from 8-bit RGBA rows that may be padded beyond width * 4 bytes.

    Read (Apple CGImageGetBytesPerRow): CoreGraphics may align rows, so the row stride must
    come from the image, not from its width; ignoring it skews every row after the first.
    """
    from PIL import Image
    if bytes_per_row < width * 4 or len(data) < bytes_per_row * height:
        raise ValueError('captured frame buffer is smaller than its reported geometry')
    return Image.frombuffer('RGBA', (width, height), data, 'raw', 'RGBA',
                            bytes_per_row, 1).convert('RGB')


def list_devices():
    """Human-readable muxed capture devices (name + modelID only; never unique IDs)."""
    AVFoundation, _, _, _ = _frameworks()
    return [{'name': d.localizedName(), 'model': str(d.modelID())}
            for d in _muxed_devices(AVFoundation)]


def _frameworks():
    try:
        import AVFoundation
        import CoreMedia
        import CoreMediaIO
        import Quartz
    except ImportError as error:
        raise UsbCaptureUnavailable(
            'USB capture needs PyObjC: uv pip install pyobjc-framework-AVFoundation '
            'pyobjc-framework-CoreMedia pyobjc-framework-CoreMediaIO pyobjc-framework-CoreImage '
            'pyobjc-framework-Quartz pyobjc-framework-libdispatch') from error
    return AVFoundation, CoreMedia, CoreMediaIO, Quartz


def _muxed_devices(AVFoundation):
    try:
        session = AVFoundation.AVCaptureDeviceDiscoverySession.\
            discoverySessionWithDeviceTypes_mediaType_position_(
                [AVFoundation.AVCaptureDeviceTypeMuxed],
                AVFoundation.AVMediaTypeVideo,
                AVFoundation.AVCaptureDevicePositionUnspecified)
        return list(session.devices())
    except AttributeError:
        # Fallback for older SDKs: filter the legacy enumeration by muxed media type.
        return [d for d in AVFoundation.AVCaptureDevice.devices()
                if d.hasMediaType_(AVFoundation.AVMediaTypeMuxed)]


class AVFoundationBackend:
    """Real backend. NOT covered by unit tests; verified manually with scripts/usb_capture_probe.py.

    Read (Apple CoreMediaIO docs): kCMIOHardwarePropertyAllowScreenCaptureDevices on the CMIO
    system object makes connected iPhones appear as AVCaptureDevices of muxed device type.
    """

    def __init__(self, *, clock=time.time, fps_window=5.0):
        self._av, self._cm, self._cmio, self._quartz = _frameworks()
        self.clock, self.fps_window = clock, fps_window
        self._lock = threading.Lock()
        self._latest = (None, None)  # (PIL.Image, capture timestamp)
        self._times = deque(maxlen=600)
        self._session = None
        self._device = None
        self._rediscover_at = 0.0

    def _allow_screen_capture_devices(self):
        cmio = self._cmio
        element = getattr(cmio, 'kCMIOObjectPropertyElementMain',
                          getattr(cmio, 'kCMIOObjectPropertyElementMaster', 0))
        address = cmio.CMIOObjectPropertyAddress(
            cmio.kCMIOHardwarePropertyAllowScreenCaptureDevices,
            cmio.kCMIOObjectPropertyScopeGlobal, element)
        import struct
        # Measured 2026-10-12 (pyobjc 12.0): a None qualifier raises "TypeError: converting to a
        # C array"; an empty bytes qualifier works (OSStatus 0).
        status = cmio.CMIOObjectSetPropertyData(
            cmio.kCMIOObjectSystemObject, address, 0, b'', 4, struct.pack('I', 1))
        if status != 0:
            raise UsbCaptureUnavailable(f'CMIO screen-capture enable failed: OSStatus {status}')

    def _find_device(self):
        for device in _muxed_devices(self._av):
            model = str(device.modelID())
            if 'iPhone' in model or 'iPhone' in (device.localizedName() or ''):
                return device
        return None

    def _ensure_device(self):
        now = time.monotonic()
        if self._device is not None and self._device.isConnected():
            return
        if now < self._rediscover_at:  # Replug rediscovery is throttled; status polls at ~10 Hz.
            return
        self._rediscover_at = now + 1.0
        device = self._find_device()
        if device is None:
            self._device = None
            return
        if device is not self._device:
            self._stop_session()
            self._device = device
            self._start_session(device)

    def _start_session(self, device):
        av = self._av
        session = av.AVCaptureSession.alloc().init()
        source, error = av.AVCaptureDeviceInput.deviceInputWithDevice_error_(device, None)
        if source is None:
            raise UsbCaptureUnavailable(f'cannot open the iPhone capture device: {error}')
        output = av.AVCaptureVideoDataOutput.alloc().init()
        output.setAlwaysDiscardsLateVideoFrames(True)
        backend = self
        try:
            from Foundation import NSObject
            from Dispatch import dispatch_queue_create

            class _Delegate(NSObject):
                def captureOutput_didOutputSampleBuffer_fromConnection_(self, out, sample, conn):
                    backend._on_sample(sample)

            queue = dispatch_queue_create(b'iphone-hand.usb-capture', None)
            delegate = _Delegate.alloc().init()
        except ImportError as error:
            raise UsbCaptureUnavailable(
                'USB capture needs pyobjc-framework-libdispatch (uv pip install '
                'pyobjc-framework-libdispatch)') from error
        output.setSampleBufferDelegate_queue_(delegate, queue)
        session.addInput_(source)
        session.addOutput_(output)
        session.startRunning()
        self._delegate, self._output, self._session = delegate, output, session  # retain

    def _on_sample(self, sample):
        image = self._image_from_sample(sample)
        if image is None:
            return
        with self._lock:
            self._latest = (image, self.clock())
            self._times.append(time.monotonic())

    def _image_from_sample(self, sample):
        """CMSampleBuffer -> PIL Image via CoreImage/CoreGraphics (format conversion included)."""
        import CoreImage
        buffer = self._cm.CMSampleBufferGetImageBuffer(sample)
        if buffer is None:
            return None
        ci = CoreImage.CIImage.imageWithCVPixelBuffer_(buffer)
        # A CIContext is expensive to create; one per backend, not one per frame.
        if getattr(self, '_ci_context', None) is None:
            self._ci_context = CoreImage.CIContext.context()
        cg = self._ci_context.createCGImage_fromRect_(ci, ci.extent())
        if cg is None:
            return None
        quartz = self._quartz
        data = quartz.CGDataProviderCopyData(quartz.CGImageGetDataProvider(cg))
        # Estimated (unverified on target): CIContext renders RGBA premultiplied here.
        return rgba_image(bytes(data), quartz.CGImageGetWidth(cg), quartz.CGImageGetHeight(cg),
                          quartz.CGImageGetBytesPerRow(cg))

    def _stop_session(self):
        if self._session is not None:
            self._session.stopRunning()
        self._session = self._output = self._delegate = None

    def start(self):
        self._allow_screen_capture_devices()
        self._ensure_device()

    def stop(self):
        self._stop_session()
        self._device = None

    def running(self):
        return self._session is not None and self._session.isRunning()

    def device_present(self):
        self._ensure_device()
        return self._device is not None and self._device.isConnected()

    def device_name(self):
        return self._device.localizedName() if self._device is not None else None

    def latest(self):
        with self._lock:
            return self._latest

    def fps(self):
        with self._lock:
            if len(self._times) < 2:
                return 0.0
            span = self._times[-1] - self._times[0]
            return (len(self._times) - 1) / span if span > 0 else 0.0


class UsbCaptureScreen:
    """Screen source with the AirPlayScreen interface, fed by AVFoundation USB capture.

    Same mirroring rule as AirPlayScreen (airplay.py:114): iOS may pause the stream on a still
    or locked screen, so while the device is connected the newest frame stays valid however old.
    """
    frames_native = True  # Phone.phone_rect may match exact native pixels (estimated).

    def __init__(self, *, backend=None, stale_after=2.0, clock=time.time, pt_scale=0.0):
        self.backend = backend if backend is not None else AVFoundationBackend()
        self.stale_after, self.clock, self.pt_scale = stale_after, clock, pt_scale
        self.last_grab_ms = None
        self.rect = None
        self.backend.start()

    def close(self):
        self.backend.stop()

    def status(self):
        _, ts = self.backend.latest()
        age = max(0.0, self.clock() - ts) if ts is not None else None
        device = self.backend.device_present()
        fresh = age is not None and age < self.stale_after
        running = self.backend.running()
        return {'mode': 'usb', 'mirroring': bool(running and (fresh or device)),
                'client_connected': device, 'device': self.backend.device_name(),
                'frame_age_s': age, 'fps': self.backend.fps() if fresh else 0.0,
                'last_grab_ms': self.last_grab_ms,
                'pt_size': [self.rect.pt_width, self.rect.pt_height] if self.rect else None}

    def grab(self, *, newest=False):
        start = time.perf_counter()
        frame, ts = self.backend.latest()
        if frame is None:
            raise FrameUnavailable('Waiting for USB capture frames (device connected? unlocked?)')
        if not self.backend.device_present() and self.clock() - ts >= self.stale_after:
            raise FrameUnavailable('USB capture frames are stale and the iPhone is disconnected')
        self.rect = PhoneRect(0, 0, *frame.size, *_pt_size(frame.size, self.pt_scale))
        self.last_grab_ms = (time.perf_counter() - start) * 1000
        return frame
