from PIL import Image, ImageDraw, ImageFont
import pytesseract
import os

os.makedirs('tmp', exist_ok=True)
path = os.path.join('tmp', 'ocr_test.png')
img = Image.new('RGB', (500, 120), color='white')
d = ImageDraw.Draw(img)
# Use default font for portability
try:
    f = ImageFont.truetype('arial.ttf', 36)
except Exception:
    f = ImageFont.load_default()
d.text((10, 30), 'iPhone 15 128GB Black', font=f, fill=(0, 0, 0))
img.save(path)

print('Saved test image to:', path)
try:
    raw = pytesseract.image_to_string(Image.open(path), config='--psm 3')
    print('OCR RAW:')
    print(raw)
except Exception as e:
    print('OCR execution failed:', e)
