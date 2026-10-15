from io import BytesIO
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
import pytest

from iphone_hand.elements import Elements, OCRUnavailable, TextBox, badges, password_prompt

AIRPLAY = Path(__file__).resolve().parents[2]/'fixtures/airplay'


class FakeOCR:
    def __init__(self, boxes=()):
        self.boxes = list(boxes)

    def read(self, image):
        return self.boxes


@pytest.mark.parametrize('width', [750, 1126])
def test_real_badges_and_pointer(width):
    labels = json.loads((AIRPLAY/'labels.json').read_text())['home-voicecontrol-pointer.jpg']
    with Image.open(AIRPLAY/'home-voicecontrol-pointer.jpg') as im:
        image = im.convert('RGB').resize((width, round(im.height*width/im.width)))
    result = Elements(FakeOCR()).analyze(image)
    numbers = {int(i['id'][2:]) for i in result['elements'] if i['type'] == 'vc_item'}
    assert len(numbers.intersection(range(1, labels['voice_control_numbers']+1))) >= 20
    assert math.dist((result['pointer']['x'],result['pointer']['y']), labels['pointer_pt']) < 1
    required = {'id','text','type','bounds','center','size','clickable','editable','enabled',
                'checked','focused','selected','scrollable','longClickable'}
    for item in result['elements']:
        assert required <= item.keys()
        assert isinstance(item['bounds'],str)
        assert item['center'][0] > item['badge_bounds'][2]
        assert item['center'][1] > item['badge_bounds'][3]
        assert item['size'][0] > 0 and item['size'][1] > 0
    marked = Elements.annotate(image,result)
    assert marked.size == image.size
    assert not np.array_equal(np.asarray(marked),np.asarray(image))


def test_badges_on_independent_synthetic_layout():
    # Render template glyphs in relocated badges; no fixture coordinates or labels in detector.
    from iphone_hand import elements
    data=json.loads(Path(elements.__file__).with_name('digit_templates.json').read_text())
    image=Image.new('RGB',(1126,2434),'black')
    draw=ImageDraw.Draw(image)
    for n,(x,y) in enumerate(((100,600),(700,400),(300,900)),start=1):
        draw.rounded_rectangle((x-14,y-11,x+50,y+47),radius=12,fill=(85,85,85))
        glyph=Image.new('RGB',(24,36),'white')
        mask=Image.fromarray(np.array([[int(v)*255 for v in row] for row in data[str(n)]],dtype=np.uint8))
        image.paste(glyph,(x,y),mask)
    assert {b['number'] for b in badges(image)} == {1,2,3}
    assert badges(Image.new('RGB',(750,1624),'white')) == []


def test_merge_and_ocr_only():
    image=Image.open(AIRPLAY/'home-voicecontrol-pointer.jpg').convert('RGB')
    reader=FakeOCR([TextBox('Settings',(570,1800,790,1840)),
                    TextBox('Status',(600,20,720,80)),TextBox('Elsewhere',(800,20,950,80))])
    result=Elements(reader).analyze(image)
    assert next(i for i in result['elements'] if i['id']=='vc18')['text']=='Settings'
    assert {i['text'] for i in result['elements'] if i['type']=='text'}=={'Status','Elsewhere'}
    assert result['ocr_available']


@pytest.mark.parametrize('text',['Password','Enter PASSCODE','Hasło','Kod','Password required'])
def test_password_guard_words(text):
    result=Elements(FakeOCR([TextBox(text,(0,0,100,30))])).analyze(Image.new('RGB',(750,1624)))
    assert password_prompt(result)


def test_missing_optional_ocr():
    class Missing:
        def read(self,image):raise OCRUnavailable('missing extra')
    result=Elements(Missing()).analyze(Image.new('RGB',(750,1624)))
    assert not result['ocr_available'] and result['ocr_error']
    assert not password_prompt({'elements':[{'text':'Kodak'}]})


def test_rapidocr_adapter_with_stub(monkeypatch):
    import sys,types
    from iphone_hand.elements import RapidOCRReader
    calls=[]
    class Engine:
        def __init__(self,params):calls.append(params)
        def __call__(self,image):
            assert isinstance(image,Image.Image)
            return types.SimpleNamespace(boxes=np.array([[[1,2],[30,2],[30,12],[1,12]]]),
                                         txts=['Hello'],scores=[.9])
    monkeypatch.setitem(sys.modules,'rapidocr',types.SimpleNamespace(RapidOCR=Engine))
    reader=RapidOCRReader()
    assert reader.read(Image.new('RGB',(40,20)))==[TextBox('Hello',(1,2,30,12),.9)]
    reader.read(Image.new('RGB',(40,20)))
    assert len(calls)==1
    assert calls[0]['EngineConfig.onnxruntime.intra_op_num_threads']==2
