
# Tabdeal Monitor v1

ربات نسخه اول برای رصد زنده Order Book صرافی تبدیل.

## چه چیزهایی را رصد می‌کند؟
- Best Bid / Best Ask
- Spread
- قدرت نسبی حجم Bid و Ask در عمق اردربوک
- Imbalance
- بزرگ‌ترین سفارش قابل مشاهده در سمت خرید و فروش
- تغییر سریع Imbalance
- هشدار با cooldown برای جلوگیری از اسپم

نسخه اول فقط داده بازار عمومی را می‌خواند و هیچ سفارش، برداشت یا معامله‌ای انجام نمی‌دهد.

## نمادهای پیش‌فرض
ARXUSDT و BTCUSDT

برای تغییر:
`SYMBOLS=arxusdt,btcusdt,ethusdt`

## اجرای محلی
```bash
pip install -r requirements.txt
python app.py
```

## تلگرام
بعداً در Render این دو Environment Variable را اضافه کن:
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID

هیچ API Key یا Secret تبدیل برای داده عمومی این نسخه لازم نیست.

## نکته
Order Book فقط سفارش‌های قابل مشاهده را نشان می‌دهد؛ «بزرگ‌ترین سفارش قابل مشاهده» الزاماً به معنی نهنگ نیست و سفارش‌ها ممکن است حذف یا جابه‌جا شوند.
