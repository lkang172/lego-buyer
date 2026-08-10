const $ = (id) => document.getElementById(id);
const basket = [];

const money = (n) => "$" + Number(n).toFixed(2);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// ---- colors datalist ----
fetch("/api/colors")
  .then((r) => r.json())
  .then((cs) => {
    $("colors").innerHTML = cs.map((c) => `<option value="${esc(c.name)}">`).join("");
  })
  .catch(() => {});

// ---- basket management ----
$("add-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const part = $("in-part").value.trim();
  if (!part) return;
  basket.push({
    part,
    quantity: Math.max(1, parseInt($("in-qty").value, 10) || 1),
    color: $("in-color").value.trim() || null,
  });
  $("in-part").value = "";
  $("in-qty").value = "1";
  $("in-color").value = "";
  $("in-part").focus();
  renderBasket();
});

function renderBasket() {
  const table = $("basket");
  const tbody = table.querySelector("tbody");
  table.classList.toggle("hidden", basket.length === 0);
  $("basket-empty").classList.toggle("hidden", basket.length > 0);
  tbody.innerHTML = basket
    .map(
      (b, i) => `<tr>
        <td>${esc(b.part)}</td>
        <td>${b.color ? esc(b.color) : '<span class="opt">Any</span>'}</td>
        <td class="num">${b.quantity}</td>
        <td class="num"><button class="link" data-i="${i}" title="Remove">&#10005;</button></td>
      </tr>`
    )
    .join("");
  tbody.querySelectorAll("button[data-i]").forEach((btn) =>
    btn.addEventListener("click", () => {
      basket.splice(Number(btn.dataset.i), 1);
      renderBasket();
    })
  );
}

const csv = (v) => v.split(",").map((s) => s.trim()).filter(Boolean);

// ---- solve ----
$("submit").addEventListener("click", async () => {
  if (!basket.length) {
    $("status").innerHTML = `<div class="msg error">Add at least one piece first.</div>`;
    return;
  }
  const maxStores = parseInt($("f-maxstores").value, 10);
  const body = {
    parts: basket,
    filters: {
      condition: $("f-cond").value,
      min_feedback: Math.max(0, parseInt($("f-feedback").value, 10) || 0),
      only_countries: csv($("f-only").value),
      exclude_countries: csv($("f-exclude").value),
      max_stores: Number.isFinite(maxStores) && maxStores > 0 ? maxStores : null,
      runner_ups: Math.max(0, parseInt($("f-runners").value, 10) || 0),
    },
  };

  $("results").innerHTML = "";
  $("status").innerHTML = `<div class="msg info"><span class="spinner"></span>Querying BrickLink for ${basket.length} part${basket.length > 1 ? "s" : ""} and optimizing&hellip;</div>`;
  $("submit").disabled = true;

  try {
    const resp = await fetch("/api/solve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "Request failed");
    $("status").innerHTML = (data.warnings || [])
      .map((w) => `<div class="msg warn">${esc(w)}</div>`)
      .join("");
    render(data);
  } catch (err) {
    $("status").innerHTML = `<div class="msg error">${esc(err.message)}</div>`;
  } finally {
    $("submit").disabled = false;
  }
});

// ---- rendering ----
function render(data) {
  const best = data.plans[0];
  const partName = {};
  data.parts.forEach((p) => (partName[p.key] = p));

  const resolved = `<div class="msg info">Resolved ${data.parts
    .map((p) => {
      const sub = p.query && p.query.toLowerCase() !== p.part_no.toLowerCase()
        ? ` <span class="est" title="BrickLink mapped what you typed to this part">(you typed ${esc(p.query)})</span>`
        : "";
      return `<b>${esc(p.part_no)}</b> ${esc(p.color_name)} &times;${p.quantity}${sub} <span class="opt">(${p.lots_considered} listings)</span>`;
    })
    .join(" &middot; ")}</div>`;

  $("results").innerHTML =
    resolved + data.plans.map((p) => planHtml(p, best, partName)).join("");
}

function planHtml(plan, best, partName) {
  const isBest = plan.rank === 1;
  const delta = plan.total - best.total;

  // Per-piece view: one row per piece, showing which store to buy it from.
  const rows = [];
  plan.stores.forEach((s) => {
    s.lines.forEach((l) => {
      rows.push(`<tr>
        <td><b>${esc(l.part_no)}</b><br><small class="opt">${esc(l.part_name)}</small></td>
        <td>${esc(l.color_name)}</td>
        <td class="num">${l.quantity}</td>
        <td class="storecell">
          <a href="${esc(s.store_url)}?q=${encodeURIComponent(l.part_no)}" target="_blank" rel="noopener">${esc(s.store_name)}</a>
          <small>${esc(s.country)} &middot; ${s.feedback.toLocaleString()} feedback</small>
        </td>
        <td class="num">${money(l.unit_price)}</td>
        <td class="num">${money(l.subtotal)}</td>
        <td class="num">${s.days_low}&ndash;${s.days_high}d</td>
      </tr>`);
    });
  });

  const storeRows = plan.stores
    .map(
      (s) => `<tr>
        <td><a href="${esc(s.store_url)}" target="_blank" rel="noopener">${esc(s.store_name)}</a>
            <small class="opt">${esc(s.country)}</small></td>
        <td class="num">${s.lines.length}</td>
        <td class="num">${money(s.merchandise)}</td>
        <td class="num">${s.min_buy > 0 ? money(s.min_buy) : "&mdash;"}</td>
        <td class="num">${s.padding > 0 ? `<span class="est">+${money(s.padding)}</span>` : "&mdash;"}</td>
        <td class="num">${money(s.shipping)}${s.shipping_is_estimate ? ' <span class="est" title="Estimated, not fetched from the seller">~</span>' : ""}</td>
        <td class="num"><b>${money(s.total)}</b></td>
      </tr>`
    )
    .join("");

  return `<section class="plan ${isBest ? "best" : ""}">
    <div class="plan-head">
      <h3 class="plan-title">
        <span class="badge ${isBest ? "" : "alt"}">${isBest ? "BEST" : "#" + plan.rank}</span>
        ${plan.store_count} store${plan.store_count > 1 ? "s" : ""}
      </h3>
      <div class="total">${money(plan.total)}${
        !isBest ? `<span class="delta">+${money(delta)}</span>` : ""
      }</div>
    </div>
    <p class="meta">Everything arrives in roughly <b>${plan.days_low}&ndash;${plan.days_high} days</b> (slowest store sets the pace).</p>

    <div class="breakdown">
      <div><span>Parts</span><b>${money(plan.merchandise)}</b></div>
      <div><span>Shipping (est.)</span><b>${money(plan.shipping)}</b></div>
      ${plan.padding > 0 ? `<div><span>Filler for minimums</span><b>${money(plan.padding)}</b></div>` : ""}
      <div><span>Total</span><b>${money(plan.total)}</b></div>
    </div>

    <table>
      <thead><tr>
        <th>Piece</th><th>Color</th><th class="num">Qty</th><th>Buy from</th>
        <th class="num">Unit</th><th class="num">Cost</th><th class="num">Est. delivery</th>
      </tr></thead>
      <tbody>${rows.join("")}</tbody>
    </table>

    <details ${isBest ? "open" : ""}>
      <summary>Per-store breakdown</summary>
      <table>
        <thead><tr>
          <th>Store</th><th class="num">Lots</th><th class="num">Parts</th>
          <th class="num">Min. buy</th><th class="num">Filler</th><th class="num">Shipping</th><th class="num">Total</th>
        </tr></thead>
        <tbody>${storeRows}</tbody>
      </table>
    </details>
  </section>`;
}
