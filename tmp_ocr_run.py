import pytesseract
from PIL import Image
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
print('Using tesseract cmd:', pytesseract.pytesseract.tesseract_cmd)
print('OCR result:')
print(pytesseract.image_to_string(Image.open('tmp/ocr_test.png'), config='--psm 3'))
