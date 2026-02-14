import time
import spidev
import RPi.GPIO as GPIO
from PIL import Image, ImageDraw, ImageFont

# Waveshare 1.3" HAT Pin Mapping
RST_PIN = 27
DC_PIN  = 25
BL_PIN  = 24
CS_PIN  = 8

class LCDDisplay:
    def __init__(self):
        # Button Pin Definitions
        # 1. SET THE MODE FIRST
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)

        # 2. Pin Definitions
        self.RST_PIN = 27
        self.DC_PIN  = 25
        self.BL_PIN  = 24
        self.CS_PIN  = 8
        self.KEY1_PIN = 21
        self.KEY2_PIN = 20
        self.KEY3_PIN = 16
        self.JOY_CTR  = 13
        # Setup GPIO for buttons as Inputs with Pull-up resistors
        # Pull-up means the pin is HIGH by default and goes LOW when pressed
        # 3. Setup LCD Control Pins
        GPIO.setup(self.RST_PIN, GPIO.OUT)
        GPIO.setup(self.DC_PIN, GPIO.OUT)
        GPIO.setup(self.BL_PIN, GPIO.OUT)
        GPIO.output(self.BL_PIN, GPIO.HIGH)
        # 4. Setup Button Pins
        for pin in [self.KEY1_PIN, self.KEY2_PIN, self.KEY3_PIN, self.JOY_CTR]:
            # Fixed the typo here: PUD_DOWN instead of PUP_LOW
            pud = GPIO.PUD_DOWN if pin == self.JOY_CTR else GPIO.PUD_UP
            GPIO.setup(pin, GPIO.IN, pull_up_down=pud)
            

        
        # GPIO Setup
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(RST_PIN, GPIO.OUT)
        GPIO.setup(DC_PIN, GPIO.OUT)
        GPIO.setup(BL_PIN, GPIO.OUT)
        GPIO.output(BL_PIN, GPIO.HIGH) # Backlight ON

        # SPI Setup
        self.spi = spidev.SpiDev()
        self.spi.open(0, 0)
        self.spi.max_speed_hz = 40000000 # 40MHz
        self.spi.mode = 0b00


        self.command(0x36) # MADCTL (Memory Access Control)
        # 0x00 is default, 0x70 is 90 deg, 0x60 is 90 deg clockwise with different mirroring
        # Try 0x00 first with the PIL rotation; if shifted, try 0x70.
        self.data(0x00) 
        
        self.command(0x3A) # Interface Pixel Format
        self.data(0x05) 
        self.command(0x21) # Display Inversion On (Most Waveshare LCDs need this)
        self.command(0x11) # Sleep Out
        time.sleep(0.1)
        self.command(0x29) # Display On
        
        self.width = 240
        self.height = 240
        self.init_display()
        
        # Prepare drawing canvas
        self.image = Image.new('RGB', (self.width, self.height), (0, 0, 0))
        self.draw = ImageDraw.Draw(self.image)
        font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        try:
            self.font_header = ImageFont.truetype(font_path, 32)
            self.font_title = ImageFont.truetype(font_path, 28)
            self.font_status = ImageFont.truetype(font_path, 22)
        except:
            self.font_header = self.font_title = self.font_status = ImageFont.load_default()


    def set_callbacks(self, key1_func=None, key2_func=None, key3_func=None):
        """Assign Python functions to be called when buttons are pressed."""
        pins = {
            self.KEY1_PIN: key1_func,
            self.KEY2_PIN: key2_func,
            self.KEY3_PIN: key3_func
        }

        for pin, func in pins.items():
            if func:
                try:
                    # Remove existing detection to avoid "Failed to add edge detection"
                    GPIO.remove_event_detect(pin)
                except:
                    pass # Pin wasn't being detected, which is fine
                
                try:
                    GPIO.add_event_detect(pin, GPIO.FALLING, callback=func, bouncetime=300)
                except RuntimeError as e:
                    print(f"Warning: Could not add edge detection on pin {pin}: {e}")            

    def command(self, cmd):
        GPIO.output(DC_PIN, GPIO.LOW)
        self.spi.writebytes([cmd])

    def data(self, val):
        GPIO.output(DC_PIN, GPIO.HIGH)
        self.spi.writebytes([val])

    def init_display(self):
        """Hard reset and ST7789 init sequence."""
        GPIO.output(RST_PIN, GPIO.HIGH)
        time.sleep(0.01)
        GPIO.output(RST_PIN, GPIO.LOW)
        time.sleep(0.01)
        GPIO.output(RST_PIN, GPIO.HIGH)
        time.sleep(0.01)

        self.command(0x11) # Sleep out
        time.sleep(0.12)
        self.command(0x36) # Memory Access Control
        self.data(0x00)    # Direction
        self.command(0x3A) # Interface Pixel Format
        self.data(0x05)    # 16-bit
        self.command(0x29) # Display on

    def show(self):
        """Push the PIL image to the hardware with 90-degree clockwise rotation."""
        # 1. Rotate the image 90 deg clockwise
        img = self.image.transpose(Image.ROTATE_270)
        
        # 2. Set Column Address (0 to 239)
        self.command(0x2A) 
        self.data(0x00); self.data(0x00) # Start Col High/Low
        self.data(0x00); self.data(0xEF) # End Col High/Low (239)

        # 3. Set Row Address (0 to 239)
        self.command(0x2B) 
        self.data(0x00); self.data(0x00) # Start Row High/Low
        self.data(0x00); self.data(0xEF) # End Row High/Low (239)

        # 4. Write to RAM
        self.command(0x2C)
        
        pix = img.load()
        buffer = []
        for y in range(self.height):
            for x in range(self.width):
                r, g, b = pix[x, y]
                color = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
                buffer.append((color >> 8) & 0xFF)
                buffer.append(color & 0xFF)
        
        GPIO.output(DC_PIN, GPIO.HIGH)
        for i in range(0, len(buffer), 4096):
            self.spi.writebytes(buffer[i:i+4096])
    def update_status(self, title, status, progress=None):
        # Clear screen
        self.draw.rectangle((0, 0, 240, 240), fill=(0, 0, 0))
        
        # Header - Moved slightly down
        self.draw.text((10, 10), "FLYSTO SYNCER", font=self.font_header, fill=(0, 255, 255))
        
        # Title - Blue/Yellow section
        self.draw.text((10, 60), title, font=self.font_title, fill=(255, 255, 0))
        
        # Status - Wrap text logic or just a single large line
        # We increase the Y-offset to 110 so it doesn't hit the title
        self.draw.text((10, 110), status[:15], font=self.font_status, fill=(255, 255, 255))
        
        if progress is not None:
            # Make the progress bar thicker too
            self.draw.rectangle((20, 190, 220, 215), outline=(255, 255, 255), width=2)
            self.draw.rectangle((22, 192, 22 + int(196 * progress), 213), fill=(0, 255, 0))
        
        self.show()


    def clear(self):
        self.draw.rectangle((0, 0, self.width, self.height), fill=(0, 0, 0))     
