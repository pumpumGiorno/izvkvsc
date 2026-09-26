/*
 * Диагностика для vk_export.js: запускать в консоли браузера на странице с музыкой VK,
 * если экспорт не находит треки или берёт не те названия. Выводит устройство первых
 * трёх строк списка (и строки с window.VK2SC_FIND, если задать), копирует результат
 * в буфер обмена. Ничего не отправляет по сети; длинные хэши заменяются на «…».
 *
 * Найти конкретный трек: сначала выполните window.VK2SC_FIND = "часть названия или исполнителя",
 * пролистайте список до него, затем вставьте этот скрипт.
 */
(() => {
  const AUTH = '[data-testid$="_Authors"], .audio_row__performers, a[href*="/artist/"]';
  const DURATION_RE = /^(?:\d{1,2}:)?\d{1,2}:\d{2}$/;
  const mask = (s) => s.replace(/\b(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{16,}\b/g, "…");
  const durations = (el) =>
    [...el.querySelectorAll("*")].filter((e) => !e.children.length && DURATION_RE.test(e.textContent.trim())).length;
  // Строка — самый верхний предок исполнителя, в котором ровно одна длительность.
  const rowOf = (a) => {
    let el = a;
    for (let depth = 0; el.parentElement && depth < 25; depth++) {
      if (durations(el.parentElement) > 1) break;
      el = el.parentElement;
    }
    return el;
  };
  const inPlayer = (el) => el.closest('[data-testid="TopAudioPlayer"], [data-testid="AudioPage_PlayerBlock"], .top_audio_player');
  const anchors = [...document.querySelectorAll(AUTH)].filter((a) => !inPlayer(a));
  const find = (window.VK2SC_FIND || "").toLowerCase();
  const rows = [];
  for (const a of anchors) {
    if (rows.length >= 3) break;
    if (!rows.some((r) => r.contains(a))) rows.push(rowOf(a));
  }
  // Искомый трек: сначала один проход по тексту, строка строится только для найденного элемента.
  let wanted = null;
  if (find) {
    const hit = [...document.querySelectorAll("body *")].find(
      (e) => !/^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE)$/.test(e.tagName) && !inPlayer(e) &&
        [...e.childNodes].some((n) => n.nodeType === Node.TEXT_NODE && n.nodeValue.toLowerCase().includes(find))
    );
    if (hit) wanted = rowOf(hit);
  }
  const ids = [...new Set([...document.querySelectorAll("[data-testid]")].map((e) => e.getAttribute("data-testid")))]
    .filter((t) => /track|audio|music/i.test(t));
  const leaves = (row) =>
    [...row.querySelectorAll("*")]
      .filter((e) => !e.children.length && e.textContent.trim())
      .map((e) => {
        const t = e.closest("[data-testid]");
        const tid = t && row.contains(t) ? t.getAttribute("data-testid") : "-";
        return `  <${e.tagName.toLowerCase()} class="${(e.getAttribute("class") || "").slice(0, 80)}"> testid=${tid} текст=${JSON.stringify(e.textContent.trim())}`;
      })
      .join("\n");
  const show = (r, label) =>
    `--- ${label}, видно: ${JSON.stringify(r.innerText)}\nтекстовые элементы:\n${leaves(r)}\nHTML:\n${mask(r.outerHTML).slice(0, 3000)}`;
  const out = [`Страница: ${location.pathname}; исполнителей в списке: ${anchors.length}; data-testid: ${ids.join(", ") || "(нет)"}`];
  rows.forEach((r, i) => out.push(show(r, `строка ${i + 1}`)));
  if (find) out.push(wanted ? show(wanted, `«${window.VK2SC_FIND}»`) : `--- «${window.VK2SC_FIND}» не найдена: пролистайте список до неё и запустите ещё раз`);
  const text = out.join("\n\n");
  console.log(text);
  try {
    copy(text);
    console.log("↑ Скопировано в буфер обмена — вставьте в сообщение.");
  } catch (e) {
    /* copy() есть только в консоли DevTools */
  }
})();
