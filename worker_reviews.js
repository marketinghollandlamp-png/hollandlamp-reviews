/**
 * HollandLamp — Review Uitnodigingen Worker
 * ==========================================
 * Versie: 1.1
 *
 * Secrets instellen via wrangler CLI:
 *   wrangler secret put MAGENTO_TOKEN
 *   wrangler secret put WORKER_SECRET
 *
 * Endpoints:
 *   POST /orders   → haalt complete orders op uit Magento
 *   GET  /afmelden → verwerkt afmeldlink van klant
 *
 * Mails worden NIET via deze Worker verstuurd.
 * Dat doet het Python-script rechtstreeks via SMTP (Office 365).
 */

const MAGENTO_BASE_URL = "https://www.hollandlamp.nl";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    const corsHeaders = {
      "Access-Control-Allow-Origin":  "*",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type, X-Worker-Secret",
    };

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders });
    }

    // Authenticatie voor alle endpoints behalve /afmelden
    if (url.pathname !== "/afmelden") {
      const secret = request.headers.get("X-Worker-Secret");
      if (!secret || secret !== env.WORKER_SECRET) {
        return jsonResponse({ error: "Ongeautoriseerd" }, 401, corsHeaders);
      }
    }

    try {
      if (request.method === "POST" && url.pathname === "/orders") {
        return await handleOrders(request, env, corsHeaders);
      }
      if (request.method === "GET" && url.pathname === "/afmelden") {
        return await handleAfmelden(request, env);
      }
      return jsonResponse({ error: "Niet gevonden" }, 404, corsHeaders);

    } catch (err) {
      console.error("Worker fout:", err);
      return jsonResponse({ error: "Interne fout", detail: err.message }, 500, corsHeaders);
    }
  }
};

// ═══════════════════════════════════════════════════════════════
// ENDPOINT 1: /orders
// ═══════════════════════════════════════════════════════════════

async function handleOrders(request, env, corsHeaders) {
  const body = await request.json();
  const { grens_nieuw, grens_oud } = body;

  if (!grens_nieuw || !grens_oud) {
    return jsonResponse(
      { error: "grens_nieuw en grens_oud zijn verplicht" },
      400, corsHeaders
    );
  }

  const params = new URLSearchParams({
    "searchCriteria[filter_groups][0][filters][0][field]":         "status",
    "searchCriteria[filter_groups][0][filters][0][value]":         "complete",
    "searchCriteria[filter_groups][0][filters][0][conditionType]": "eq",
    "searchCriteria[filter_groups][1][filters][0][field]":         "updated_at",
    "searchCriteria[filter_groups][1][filters][0][value]":         grens_nieuw,
    "searchCriteria[filter_groups][1][filters][0][conditionType]": "lteq",
    "searchCriteria[filter_groups][2][filters][0][field]":         "updated_at",
    "searchCriteria[filter_groups][2][filters][0][value]":         grens_oud,
    "searchCriteria[filter_groups][2][filters][0][conditionType]": "gteq",
    "searchCriteria[pageSize]":                                    "100",
    "searchCriteria[sortOrders][0][field]":                        "updated_at",
    "searchCriteria[sortOrders][0][direction]":                    "ASC",
  });

  const resp = await fetch(
    `${MAGENTO_BASE_URL}/rest/V1/orders?${params}`,
    {
      headers: {
        "Authorization": `Bearer ${env.MAGENTO_TOKEN}`,
        "Content-Type":  "application/json",
      }
    }
  );

  if (!resp.ok) {
    return jsonResponse(
      { error: "Magento API fout", status: resp.status },
      502, corsHeaders
    );
  }

  const data   = await resp.json();
  const orders = (data.items || []).map(order => ({
    order_id: order.increment_id || order.entity_id,
    email:    order.customer_email,
    voornaam: order.customer_firstname || "klant",
  }));

  return jsonResponse({ orders, totaal: orders.length }, 200, corsHeaders);
}

// ═══════════════════════════════════════════════════════════════
// ENDPOINT 2: /afmelden
// ═══════════════════════════════════════════════════════════════

async function handleAfmelden(request, env) {
  const url   = new URL(request.url);
  const token = url.searchParams.get("token") || "";

  try {
    const decoded = decodeURIComponent(token);
    const delen   = decoded.split(":", 2);

    if (delen.length !== 2 || !delen[1].includes("@")) {
      throw new Error("Ongeldig token");
    }

    const [order_id, email] = delen;

    await env.AFMELDINGEN.put(
      `afmelding:${email.toLowerCase()}`,
      JSON.stringify({
        email:       email.toLowerCase(),
        order_id:    order_id,
        afgemeld_op: new Date().toISOString()
      })
    );

    console.log(`Afmelding: ${email} (order ${order_id})`);
    return htmlResponse(paginaSucces());

  } catch (e) {
    return htmlResponse(paginaFout(), 400);
  }
}

// ═══════════════════════════════════════════════════════════════
// HELPERS
// ═══════════════════════════════════════════════════════════════

function jsonResponse(data, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", ...extraHeaders }
  });
}

function htmlResponse(html, status = 200) {
  return new Response(html, {
    status,
    headers: { "Content-Type": "text/html;charset=UTF-8" }
  });
}

function paginaSucces() {
  return `<!DOCTYPE html>
<html lang="nl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Afgemeld – HollandLamp</title>
  <style>
    body{font-family:Arial,sans-serif;background:#f4f4f4;display:flex;
         align-items:center;justify-content:center;min-height:100vh;margin:0}
    .box{background:#fff;border-radius:8px;padding:48px 40px;max-width:480px;
         text-align:center;box-shadow:0 2px 12px rgba(0,0,0,.1)}
    h1{color:#1a2a3a;font-size:22px;margin:16px 0 12px}
    p{color:#666;line-height:1.6}
    a{color:#e8710a}
    img{max-height:40px;margin-bottom:24px}
  </style>
</head>
<body>
  <div class="box">
    <img src="https://www.hollandlamp.nl/media/logo/stores/1/page-logo.jpg" alt="HollandLamp">
    <div style="font-size:48px">✅</div>
    <h1>U bent afgemeld</h1>
    <p>U ontvangt geen review-uitnodigingen meer van HollandLamp.<br><br>
    Vragen? <a href="mailto:info@hollandlamp.nl">info@hollandlamp.nl</a>
    of <a href="tel:0722600000">072 – 26 000 00</a>.</p>
    <p style="margin-top:24px">
      <a href="https://www.hollandlamp.nl">← Terug naar HollandLamp.nl</a>
    </p>
  </div>
</body>
</html>`;
}

function paginaFout() {
  return `<!DOCTYPE html>
<html lang="nl">
<head>
  <meta charset="UTF-8">
  <title>Ongeldige link – HollandLamp</title>
  <style>
    body{font-family:Arial,sans-serif;background:#f4f4f4;display:flex;
         align-items:center;justify-content:center;min-height:100vh;margin:0}
    .box{background:#fff;border-radius:8px;padding:48px 40px;max-width:480px;
         text-align:center;box-shadow:0 2px 12px rgba(0,0,0,.1)}
    h1{color:#1a2a3a;font-size:22px;margin:16px 0 12px}
    p{color:#666;line-height:1.6}
    a{color:#e8710a}
  </style>
</head>
<body>
  <div class="box">
    <div style="font-size:48px">⚠️</div>
    <h1>Ongeldige afmeldlink</h1>
    <p>Deze link is niet geldig of al eerder gebruikt.<br><br>
    Stuur een mail naar
    <a href="mailto:info@hollandlamp.nl?subject=Afmelden review-uitnodigingen">
      info@hollandlamp.nl
    </a> om u af te melden.</p>
    <p style="margin-top:24px">
      <a href="https://www.hollandlamp.nl">← Terug naar HollandLamp.nl</a>
    </p>
  </div>
</body>
</html>`;
}
