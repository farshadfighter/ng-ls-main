# هدف امنیتی (Security Target) — سرور لایسنس NGCorion

> **وضعیت:** پیش‌نویس برای آزمایشگاه ارزیابی امنیتی مورد تأیید مرکز افتا؛ ساختار طبق ISO/IEC 15408 (CC 3.1 R5).
> هر ادعا به کد مشخصی ارجاع دارد.

| مورد | مقدار |
|---|---|
| محصول (TOE) | سرور لایسنس NGCorion (API + کنسول مدیریت) |
| نسخه | 2.0.0 (تگ گیت `v2.0.0`، `VERSION` در `license_server/app/main.py`) |
| سطح پیشنهادی | EAL2 + ALC_FLR.1 |
| کنترل‌های خلاصه | `SECURITY.md` |

## ۱. معرفی

سرور لایسنس، لایسنس‌های NGCorion را صادر، فعال‌سازی، اعتبارسنجی و ابطال می‌کند. هر پاسخ لایسنس با کلید
خصوصی Ed25519 امضا می‌شود؛ NGCorion فقط پاسخِ دارای امضای معتبر را می‌پذیرد. مدیران از کنسول وب
(`license-admin-ui`) لایسنس می‌سازند، طرح را تغییر می‌دهند، فایل لایسنس آفلاین صادر می‌کنند و دنبالهٔ ممیزی را می‌بینند.

**مرز TOE:** `license_server/` (FastAPI)، `license-admin-ui/` (React + nginx)، پیکربندی `docker-compose*.yml` و `traefik/`.
**محیط:** میزبان Docker، PostgreSQL، Redis، Cloudflare Turnstile، پورتال فروش (کلاینت `/internal/portal`)، NTP.

## ۲. تهدیدها

| شناسه | تهدید |
|---|---|
| T.FORGE | جعل یا دستکاری پاسخ/فایل لایسنس برای دور زدن محدودیت. |
| T.KEY_THEFT | افشای کلید خصوصی امضا. |
| T.ADMIN_ABUSE | دسترسی غیرمجاز یا فراتر از نقش به کنسول مدیریت. |
| T.BRUTE | حدس رمز مدیران یا کلید لایسنس. |
| T.REPLAY | بازپخش پاسخ لایسنس یا توکن پورتال. |
| T.SHARING | استفادهٔ یک لایسنس روی چند ماشین. |
| T.UNDETECTED | تغییر لایسنس بدون ردپا یا پاک شدن ردپا. |
| T.EAVESDROP | شنود/دستکاری در شبکه. |

**فرض‌ها:** A.ADMIN (مدیران مورد اعتماد)، A.HOST (میزبان و کلید امضا فقط در اختیار مالک)، A.TIME، A.NETWORK.

## ۳. اهداف و الزامات کارکردی (SFR) و پیاده‌سازی (TSS)

| مؤلفه CC | الزام | پیاده‌سازی (کد) |
|---|---|---|
| **FCS_COP.1 (امضا)** | امضای Ed25519 هر پاسخ و فایل آفلاین؛ nonce کلاینت درون پاسخ امضاشده | `app/utils/license_token.py`، `app/offline.py` |
| **FCS_CKM.4 / حفاظت کلید** | کلید خصوصی فقط در متغیر محیطی میزبان؛ هرگز در لاگ، پاسخ، گیت یا ایمیج | `app/core/config.py`، `.gitignore`، `.dockerignore` |
| **FCS_COP.1 (رمز عبور)** | scrypt با نمک تصادفی | `app/core/passwords.py` |
| **FCS_COP.1 (نشست/پورتال)** | JWT HS256 با issuer/audience و کلید ≥ ۲۵۶ بیت؛ توکن پورتال تک‌مصرف (jti در Redis) | `app/core/security.py`، `app/core/portal_auth.py` |
| **FDP_ACF.1 (یک ماشین)** | لایسنس به اثرانگشت ماشین در اولین فعال‌سازی گره می‌خورد | `app/crud.py` |
| **FIA_UID.2 / FIA_UAU.2** | حساب شخصی برای هر مدیر؛ احراز هویت پیش از هر اقدام | `app/accounts.py`، `app/core/security.py` |
| **FIA_AFL.1** | قفل ۱۵ دقیقه‌ای پس از ۵ شکست؛ پاسخ یکسان و زمان یکسان برای همهٔ ردها | `app/accounts.py` |
| **FIA_SOS.1** | رمز ≥ ۱۲ نویسه، سه نوع نویسه، بدون نام کاربری | `app/core/passwords.py` |
| **FIA_UAU.6** | تغییر اجباری رمز موقت پیش از هر کار | `must_change_password` |
| **FMT_SMR.1** | نقش‌های admin، operator، auditor | `app/models.py` (`AdminRole`) |
| **FMT_MOF.1 / FMT_MTD.1** | بررسی نقش در هر مسیر | `require_reader/operator/admin/auditor` |
| **FMT_SMF.1** | مدیریت حساب، لایسنس، طرح، آفلاین، بازنشانی ماشین | روترهای `admin*.py`، `app/cli.py` |
| **FTA_SSL.3 / FTA_SSL.4** | پایان نشست پس از ۱۵ دقیقه بیکاری و ۸ ساعت حداکثر؛ خروج سمت سرور | `admin_sessions`، `app/core/security.py` |
| **FTA_TAB.1** | اطلاعیهٔ پیش از ورود | `ADMIN_LOGIN_BANNER` |
| **FAU_GEN.1 / FAU_GEN.2** | ثبت ورود/خروج/قفل، تغییرات حساب و لایسنس، فایل آفلاین، سفارش پورتال، فعال‌سازی‌ها و ردها، خروجی ممیزی، شروع سرور — با هویت، نقش، نشانی، زمان، نتیجه | `app/audit.py` |
| **FAU_SAR.1 / FAU_SAR.2** | مرور و خروجی CSV فقط برای auditor و admin | `app/routers/admin_audit.py` |
| **FAU_STG.1** | تریگر پایگاه داده UPDATE/DELETE را رد می‌کند | مهاجرت `008`، `models.AUDIT_GUARD_SQL` |
| **FTP_TRP.1 / FTP_ITC.1** | TLS 1.2+ با رمزهای AEAD؛ گواهی Let's Encrypt برای lic.ngcorion.com؛ HSTS | `license-admin-ui/nginx.conf`، `traefik/dynamic.yml`، `ACME.md` |
| **FPT_FLS / دسترس‌پذیری** | محدودیت نرخ به‌ازای نشانی (اتمیک، مقاوم در برابر جعل X-Forwarded-For)، محدودیت اندازهٔ درخواست | `app/middleware/rate_limit.py`، nginx |
| **FPT_STM.1** | زمان سیستم (NTP، محیط) | — |

## ۴. الزامات تضمین — EAL2 + ALC_FLR.1

| مؤلفه | مدرک |
|---|---|
| ADV_ARC.1 / ADV_FSP.2 / ADV_TDS.1 | `docs/fa/ARCHITECTURE.md` (معماری امنیتی، فارسی)، `license_server/LICENSE_PLANS_SUMMARY.md`، این سند |
| AGD_OPE.1 / AGD_PRE.1 | `docs/fa/ADMIN_GUIDE.md` (نصب و راهبری، همهٔ نقش‌ها، فارسی)، `ACME.md`، راهنمای درون کنسول (Guide) |
| ALC_CMC.2 / ALC_CMS.2 | گیت با تگ نسخه، `CHANGELOG.md`، CI |
| ALC_FLR.1 | `SECURITY.md`، Dependabot، `pip-audit` و `npm audit` در CI |
| ATE_COV.1 / ATE_FUN.1 | آزمون‌های خودکار `license_server/tests/` (روی PostgreSQL و Redis واقعی در CI) |
| AVA_VAN.2 | بازبینی کد امن و WSTG (کامیت `Security review` و همین سند)؛ آزمون نفوذ مستقل توسط آزمایشگاه |
