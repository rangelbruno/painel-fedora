// Lista as abas e adormece (descarta) as paradas, a pedido do Painel do PC.
const PAINEL = /^https?:\/\/(127\.0\.0\.1|localhost):8765\//;
const CHAMADA = /meet\.google\.com|zoom\.us|teams\.microsoft\.com|whereby\.com/;

function info(t) {
  return {
    id: t.id, windowId: t.windowId, title: t.title, url: t.url || t.pendingUrl || "",
    active: t.active, pinned: t.pinned, audible: t.audible, discarded: t.discarded,
    lastAccessed: t.lastAccessed || null,
  };
}

// Pode adormecer sem atrapalhar: não é a aba visível, não toca som,
// não é o painel, não é página interna e não parece chamada de vídeo.
function podeAdormecer(t, incluirFixadas) {
  const url = t.url || "";
  return !t.active && !t.audible && !t.discarded && !PAINEL.test(url) &&
    !url.startsWith("chrome") && !CHAMADA.test(url) && (incluirFixadas || !t.pinned);
}

async function adormecer(abas) {
  let ok = 0;
  for (const t of abas) {
    try { if (await chrome.tabs.discard(t.id)) ok++; } catch { /* aba fechou ou recusou */ }
  }
  return ok;
}

chrome.runtime.onMessage.addListener((msg, _sender, responder) => {
  (async () => {
    const todas = await chrome.tabs.query({});
    if (msg.acao === "abas") return { abas: todas.map(info) };
    if (msg.acao === "adormecer-paradas") {
      const alvo = todas.filter((t) => podeAdormecer(t, msg.args?.fixadas));
      return { adormecidas: await adormecer(alvo), candidatas: alvo.length };
    }
    if (msg.acao === "adormecer-ids") {
      const ids = new Set(msg.args?.ids || []);
      const alvo = todas.filter((t) => ids.has(t.id) && !t.active && !t.discarded && !PAINEL.test(t.url || ""));
      return { adormecidas: await adormecer(alvo), candidatas: alvo.length };
    }
    return { erro: "ação desconhecida" };
  })().then(responder, (e) => responder({ erro: String(e) }));
  return true; // resposta assíncrona
});
