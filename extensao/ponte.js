// Ponte entre a página do painel e a extensão (só roda em 127.0.0.1:8765).
window.addEventListener("message", (ev) => {
  if (ev.source !== window || !ev.data || ev.data.painel !== "pedido") return;
  const { id, acao, args } = ev.data;
  chrome.runtime.sendMessage({ acao, args }, (resposta) => {
    const erro = chrome.runtime.lastError ? chrome.runtime.lastError.message : null;
    window.postMessage({ painel: "resposta", id, resposta, erro }, "*");
  });
});
window.postMessage({ painel: "extensao-pronta" }, "*");
