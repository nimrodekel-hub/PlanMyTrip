# פרוקסי Cloudflare Worker — הקמה (5 דקות, חינם)

מטרה: פרוקסי אישי ואמין שמעביר בקשות ל-Google Maps (`/maps/preview/place`,
`entitylist/getlist`) בלי הגבלת קצב — מחליף את הפרוקסים הציבוריים שנכשלים תחת עומס.
**100,000 בקשות ביום בחינם.**

## שלב 1 — צור חשבון Cloudflare (חינם)
1. היכנס ל-https://dash.cloudflare.com/sign-up והירשם (חינם, ללא כרטיס אשראי).
2. אמת את האימייל.

## שלב 2 — צור Worker
1. בתפריט הצד: **Workers & Pages** → **Create application** → **Create Worker**.
2. תן שם (למשל `planmytrip-proxy`) → **Deploy**.
3. אחרי הפריסה לחץ **Edit code**.
4. מחק את כל הקוד הקיים והדבק את הקוד הבא:

```js
export default {
  async fetch(request) {
    const CORS = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, OPTIONS",
      "Access-Control-Allow-Headers": "*",
    };
    if (request.method === "OPTIONS") return new Response(null, { headers: CORS });

    const target = new URL(request.url).searchParams.get("url");
    if (!target) return new Response("missing url param", { status: 400, headers: CORS });

    // אבטחה: רק כתובות גוגל מותרות
    let host;
    try { host = new URL(target).hostname; } catch { return new Response("bad url", { status: 400, headers: CORS }); }
    if (!/(^|\.)google\.com$/.test(host) && !/(^|\.)googleapis\.com$/.test(host)) {
      return new Response("host not allowed", { status: 403, headers: CORS });
    }

    try {
      const r = await fetch(target, {
        headers: { "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)" },
      });
      const body = await r.text();
      return new Response(body, {
        status: r.status,
        headers: { ...CORS, "Content-Type": "text/plain; charset=utf-8" },
      });
    } catch (e) {
      return new Response("proxy error: " + e.message, { status: 502, headers: CORS });
    }
  },
};
```

5. לחץ **Deploy** (כפתור כחול למעלה).

## שלב 3 — העתק את הכתובת והדבק באפליקציה
1. בעמוד ה-Worker תראה כתובת כמו `https://planmytrip-proxy.<your-name>.workers.dev`.
2. באפליקציה (v2) → לשונית **🔗 חיבוריות** → שדה **⚡ פרוקסי אישי**.
3. הדבק את הכתובת ובסוף הוסף `/?url={url}` — לדוגמה:
   ```
   https://planmytrip-proxy.amit.workers.dev/?url={url}
   ```
   ⚠️ חובה שיהיה `{url}` בסוף — האפליקציה מחליפה אותו בכתובת היעד.
4. לחץ מחוץ לשדה לשמירה. אמור להופיע "✅ פרוקסי אישי מוגדר".

## שלב 4 — בדיקה
ייבא רשימה מגוגל מפות. עכשיו ההעשרה תהיה מהירה ואמינה (כל 153 המקומות יקבלו
כתובת, מדינה, דירוג, תמונה ושעות) — והקיבוץ יהיה תחת מדינה אחת.

---

## אבחון
- אם עדיין נכשל: ודא שה-Worker פרוס (Deploy), ושהכתובת מסתיימת ב-`/?url={url}`.
- בדיקה ידנית בדפדפן: `https://...workers.dev/?url=https://www.google.com` →
  אמור להחזיר HTML של גוגל (לא שגיאה).
