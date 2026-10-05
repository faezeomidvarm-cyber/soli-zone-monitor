# fafi monitor — ابزار مانیتورینگ دو مرحله‌ای

این نسخه فقط هشدار می‌دهد و هیچ سفارش واقعی یا Paper Trading را خودکار ثبت نمی‌کند.

## منطق

- `ZONE_TOUCH`: قیمت خط Soli را لمس کند یا وارد مستطیل configured شود.
- `CONFIRMED_15M`: بعد از لمس، کندل ۱۵دقیقه‌ای واکنش مناسب، شکست ساختار کوتاه‌مدت و هم‌جهتی با روند 4H داشته باشد. برای محاسبه EMAهای 4H، تنظیم پیش‌فرض ۱۵ روز داده دریافت می‌کند.
- چون SVA خصوصی است، پیام مرحله دوم می‌گوید SVA را دستی روی TradingView بررسی کن. این نسخه فرمول SVA را ادعا نمی‌کند.
- بعد از سه کندل خارج از ناحیه، چرخه هشدار دوباره مسلح می‌شود.

## نصب

در PowerShell:

```powershell
cd C:\Users\fafi\Documents\Codex\2026-09-23\sba\monitor
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## اجرای آزمایشی یک‌باره

```powershell
.\.venv\Scripts\python.exe .\zone_monitor.py --once
```

برای اجرای مداوم:

```powershell
.\.venv\Scripts\python.exe .\zone_monitor.py
```

اطلاعات وضعیت در `state.json` و رویدادها در `alerts.jsonl` ذخیره می‌شوند.

## Telegram

قبل از ارسال اعلان، فایل `.env.example` را به `.env` کپی کن و مقدارها را فقط در همان فایل محلی وارد کن؛ توکن را داخل چت نفرست:

```powershell
Copy-Item .env.example .env
notepad .env
```

فایل `.env` باید این دو خط را داشته باشد:

```text
TELEGRAM_BOT_TOKEN=توکن_ربات
TELEGRAM_CHAT_ID=شناسه_چت
```

برای پیدا کردن Chat ID، بعد از فرستادن `/start` به ربات، این دستور را در PowerShell اجرا کن؛ توکن فقط روی کامپیوترت استفاده می‌شود:

```powershell
$token = Read-Host "Bot token"
$u = Invoke-RestMethod "https://api.telegram.org/bot$token/getUpdates"
$u.result[-1].message.chat.id
```

اگر این متغیرها تنظیم نشده باشند، رویدادها فقط در ترمینال چاپ می‌شوند.

## نکته مهم درباره داده

نمادهای Yahoo در `zones.json` proxy هستند: BTC/ETH از USD، طلا از COMEX و نقره از COMEX. چون نواحی روی فیدهای Binance/OANDA رسم شده‌اند، قبل از اتکا باید اختلاف قیمت بررسی شود. برای نسخه عملی، فید داده باید با فید TradingView هماهنگ شود.

محدوده مستطیل‌ها هنوز در `zones.json` وارد نشده‌اند؛ برای هر مستطیل باید `lower` و `upper` دقیق اضافه شود.

## اجرای ابری

فضای ذخیره‌سازی ابری به‌تنهایی برنامه را اجرا نمی‌کند. برای اجرای وقتی لپ‌تاپ خاموش است، یک VPS یا background worker لازم است. فایل‌های `Dockerfile` و `docker-compose.yml` برای استقرار روی چنین محیطی آماده شده‌اند.

در محیط ابری، مقدارهای Telegram را به‌عنوان Secret/Environment Variable ثبت کن، نه داخل فایل عمومی:

```text
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

سرویس باید قابلیت `restart: unless-stopped` یا معادل آن را داشته باشد. قبل از انتقال نهایی، فید داده نیز باید از proxyهای Yahoo به فید هم‌قیمت با نمادهای OANDA/Binance تغییر کند.

## آزمون رایگان با GitHub Actions

فایل `.github/workflows/monitor.yml` هر ۵ دقیقه یک اسکن مستقل اجرا می‌کند. این روش دائماً روشن نیست و زمان‌بندی GitHub تضمین لحظه‌ای ندارد. اجرای هر ۵ دقیقه در مخزن خصوصی می‌تواند سهمیهٔ رایگان Actions را سریع مصرف کند. در تنظیمات Repository Secrets این دو مقدار را بساز:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

فایل `.env` را به GitHub آپلود نکن. خود GitHub Secrets برای نگهداری مقدارهای حساس استفاده می‌شود.

## بک‌تست پژوهشی ورود

برای مقایسهٔ مدل‌های سریع، متعادل و محافظه‌کار ورود، بدون استفاده از SVA:

```powershell
python backtest_entries.py --days 30
```

این گزارش پژوهشی است؛ چون زمان ایجاد نواحی در فایل ثبت نشده، نتایج تاریخی می‌تواند دچار hindsight باشد.
