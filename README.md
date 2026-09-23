# Tabdeal Live Scanner

ربات فقط-خواندنی برای رصد زنده بازار عمومی صرافی تبدیل.

## معماری

`Tabdeal public API/WebSocket -> LiveScanner -> Read-only HTTP API -> ChatGPT / dashboard`

ربات هیچ سفارش، معامله، برداشت، ویرایش سفارش یا عملیات حساب کاربری انجام نمی‌دهد.

## داده‌های زنده

- کشف خودکار بازارهای فعال از `exchangeInfo`
- Order Book همه بازارهای فعال از WebSocket عمومی مستند تبدیل
- Best Bid / Best Ask
- Spread
- ارزش Bid و Ask در عمق انتخاب‌شده
- Imbalance و فشار خرید/فروش
- تغییر سریع Imbalance
- بزرگ‌ترین سفارش قابل مشاهده در عمق دریافت‌شده
- زمان دریافت و زمان رویداد صرافی
- تشخیص اولیه anomaly بر اساس imbalance و shift
- معاملات اخیر هر نماد از REST عمومی، هنگام درخواست API

### محدودیت مهم

API عمومی مستند تبدیل، اطلاعات خصوصی حساب را ارائه نمی‌کند. بنابراین این نسخه عمداً ادعا نمی‌کند که به پوزیشن خصوصی، سفارش‌های خصوصی، liquidation خصوصی، یا سایر داده‌هایی که منبع عمومی برایشان وجود ندارد دسترسی دارد. اگر تبدیل برای یکی از این داده‌ها endpoint عمومی/وب‌سوکت عمومی ارائه کند، می‌توان آن را بدون تغییر معماری به scanner اضافه کرد.

همچنین «بزرگ‌ترین سفارش قابل مشاهده» به معنی نهنگ بودن صاحب سفارش نیست؛ سفارش‌ها ممکن است جابه‌جا یا حذف شوند.

## API

- `GET /health` — وضعیت سرویس و اتصال
- `GET /api/capabilities` — دقیقاً چه داده‌هایی فعال است
- `GET /api/markets` — همه نمادهای فعال
- `GET /api/scanner?limit=100` — خلاصه زنده بازارها و metrics
- `GET /api/orderbook/BTCUSDT?levels=20` — اردربوک زنده یک نماد
- `GET /api/trades/BTCUSDT?limit=100` — معاملات اخیر عمومی یک نماد
- `GET /api/compare/AAVEUSDT/ARXUSDT` — مقایسه زنده دو نماد با order book، momentum، trade flow و signal tier

این endpointها فقط GET هستند و هیچ API key یا secret تبدیل را به بیرون منتشر نمی‌کنند.

## اجرای محلی

```bash
pip install -r requirements.txt
python app.py
```

سرویس روی `0.0.0.0:$PORT` اجرا می‌شود و اگر `PORT` تنظیم نشده باشد از 8000 استفاده می‌کند.

## Render

فایل `render.yaml` برای Deploy سریع آماده شده است.

برای تست اولیه می‌توان از پلن رایگان استفاده کرد؛ برای رصد 24/7 باید سرویس میزبانی‌ای انتخاب شود که sleep نکند.

## Environment Variables اختیاری

- `TABDEAL_BASE` — پیش‌فرض `https://api1.tabdeal.org`
- `TABDEAL_WS` — پیش‌فرض `wss://api1.tabdeal.org/stream/`
- `DEPTH_LEVELS` — عمق مورد استفاده برای محاسبات، پیش‌فرض 20
- `SUBSCRIBE_BATCH` — تعداد stream در هر پیام subscribe، پیش‌فرض 100
- `MARKET_REFRESH_SECONDS` — فاصله refresh فهرست بازارها، پیش‌فرض 300
- `MAX_HISTORY` — تعداد نمونه‌های imbalance برای هر بازار، پیش‌فرض 30
- `LOG_LEVEL` — پیش‌فرض INFO

## امنیت

برای داده عمومی بازار هیچ Tabdeal API secret لازم نیست و نباید secret در GitHub، کد، URL یا پاسخ API قرار گیرد.

## نکته درباره ChatGPT

برای اینکه ChatGPT بتواند داده زنده را بخواند، سرویس باید یک URL عمومی HTTPS داشته باشد و endpoint `GET /api/scanner` بدون login در دسترس باشد. این API فقط داده عمومی بازار را برمی‌گرداند.
