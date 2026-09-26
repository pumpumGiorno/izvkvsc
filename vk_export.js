/*
 * Экспорт списка аудиозаписей VK в tracks.txt — запускать в консоли браузера
 * на странице со своей музыкой (vk.com/audios…). Подробности — в README.
 *
 * Скрипт только читает страницу: ничего не отправляет по сети и ничего не
 * меняет в аккаунте. Он прокручивает список до конца, собирает треки и
 * скачивает файл «Исполнитель — Название | м:сс», по одному треку в строке.
 *
 * Вёрстка VK меняется, поэтому треки ищутся несколькими способами по очереди:
 *   1) классы audio_row__performers / audio_row__title_inner;
 *   2) атрибут data-audio со служебными данными трека;
 *   3) классы, похожие на AudioRow / TrackRow с title / artist / duration внутри;
 *   4) эвристика: строка с длительностью «м:сс» и ссылкой на исполнителя.
 * Если ничего не найдено — будет понятная ошибка, а не пустой файл.
 */
(() => {
  const OPT = Object.assign(
    {
      stepMs: 800, // пауза после каждого шага прокрутки
      idleRounds: 6, // столько шагов подряд без новых треков внизу страницы = конец списка
      maxMinutes: 20, // страховка от бесконечной прокрутки
      onlyBiggestSection: true, // брать только самый большой блок треков на странице
      download: true, // скачать tracks.txt
      fileName: "tracks.txt",
    },
    window.VK2SC_OPTIONS || {}
  );

  const DURATION_RE = /^(?:\d{1,2}:)?\d{1,2}:\d{2}$/;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const log = (...a) => console.log("%c[vk2sc]", "color:#2a5885;font-weight:bold", ...a);

  const clean = (s) =>
    String(s == null ? "" : s)
      .replace(/[​-‏⁠﻿]/g, "")
      .replace(/\s+/g, " ")
      .trim();

  const decodeEntities = (s) => {
    if (!/[&<]/.test(s)) return s;
    const doc = new DOMParser().parseFromString(`<!doctype html><body>${s}`, "text/html");
    return doc.body.textContent || "";
  };

  const textOf = (el) => (el ? clean(el.innerText || el.textContent) : "");
  const durationLeaves = (root) =>
    [...root.querySelectorAll("*")].filter((e) => !e.children.length && DURATION_RE.test(clean(e.textContent)));

  const toSeconds = (text) => {
    const t = clean(text);
    if (!DURATION_RE.test(t)) return null;
    return t.split(":").reduce((acc, p) => acc * 60 + Number(p), 0);
  };

  const fmt = (sec) => {
    if (!sec) return "";
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = String(sec % 60).padStart(2, "0");
    return h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
  };

  const parseDataAudio = (el) => {
    const raw = el && el.getAttribute && el.getAttribute("data-audio");
    if (!raw) return null;
    try {
      const d = JSON.parse(raw);
      if (!Array.isArray(d)) return null;
      let title = clean(decodeEntities(String(d[3] || "")));
      const artist = clean(decodeEntities(String(d[4] || "")));
      const sub = typeof d[16] === "string" ? clean(decodeEntities(d[16])) : "";
      if (sub && !title.toLowerCase().includes(sub.toLowerCase())) title += ` (${sub})`;
      return {
        artist,
        title,
        duration: Number(d[5]) > 0 ? Number(d[5]) : null,
        id: d[1] != null && d[0] != null ? `${d[1]}_${d[0]}` : null,
      };
    } catch (e) {
      return null;
    }
  };

  // Не берём строки из плеера и очереди воспроизведения.
  const EXCLUDE = '#top_audio_layer_place, .top_audio_layer, .audio_layer_container, .audio_page_player, [class*="top_audio_player"]';
  const SECTION = '.CatalogBlock, [class*="CatalogBlock__root"], [class*="CatalogBlock_"], section, [class*="Section"], .audio_page__audio_rows_list, ._audio_page__audio_rows_list';

  const excluded = (el) => !!el.closest(EXCLUDE);
  const ROW_LIKE =
    '[class*="AudioRow"], [class*="audio-row"], [class*="TrackRow"], [class*="track-row"], [class*="MusicTrack"], [data-testid*="audio" i], [data-testid*="track" i]';

  // ---------- стратегии ----------

  const strategies = [
    {
      name: "классы audio_row__*",
      rows: () =>
        [...document.querySelectorAll(".audio_row__performers")]
          .map((p) => p.closest(".audio_row, ._audio_row, [data-audio], [data-full-id]") || p.parentElement.parentElement)
          .filter((r, i, a) => r && a.indexOf(r) === i),
      read: (row) => {
        const perf = row.querySelector(".audio_row__performers");
        const titleBox = row.querySelector(".audio_row__title") || row;
        let title = textOf(row.querySelector(".audio_row__title_inner")) || textOf(titleBox);
        const sub = textOf(titleBox.querySelector('.audio_row__title_inner_subtitle, [class*="subtitle"]'));
        if (sub && !title.toLowerCase().includes(sub.toLowerCase())) title += ` (${sub.replace(/^\((.*)\)$/, "$1")})`;
        const data = parseDataAudio(row.closest("[data-audio]") || row);
        return {
          artist: textOf(perf),
          title,
          duration: toSeconds(textOf(row.querySelector(".audio_row__duration"))) || (data && data.duration),
          id: (data && data.id) || row.getAttribute("data-full-id"),
        };
      },
    },
    {
      name: "атрибут data-audio",
      rows: () => [...document.querySelectorAll("[data-audio]")],
      read: (row) => parseDataAudio(row),
    },
    {
      name: "классы AudioRow/TrackRow",
      rows: () => {
        const all = [...document.querySelectorAll(ROW_LIKE)];
        // Оставляем самые внешние: внутренние узлы тоже часто называются AudioRow__что-то.
        const roots = all.filter((el) => !(el.parentElement && el.parentElement.closest(ROW_LIKE)));
        return roots.filter((r) => r.querySelector('[class*="itle"]'));
      },
      read: (row) => {
        const pick = (sel) => [...row.querySelectorAll(sel)].map(textOf).find(Boolean) || "";
        const title = pick('[class*="Title"]:not([class*="Subtitle"]), [class*="title"]:not([class*="subtitle"])');
        const artist = pick(
          '[class*="erformer"], [class*="rtist"], [class*="uthor"], [class*="Subtitle"], [class*="subtitle"]'
        );
        let duration = toSeconds(pick('[class*="uration"], [class*="Time"], time'));
        if (!duration) {
          const leaf = durationLeaves(row)[0];
          duration = leaf ? toSeconds(textOf(leaf)) : null;
        }
        const data = parseDataAudio(row);
        return { artist, title, duration, id: (data && data.id) || row.getAttribute("data-id") };
      },
    },
    {
      name: "эвристика по длительности",
      heuristic: true,
      rows: () => {
        const leaves = durationLeaves(document.body);
        const rows = [];
        for (const leaf of leaves) {
          let el = leaf.parentElement;
          for (let depth = 0; el && depth < 6; depth++, el = el.parentElement) {
            if (durationLeaves(el).length > 1) break; // поднялись до списка — строки не нашли
            if (el.querySelector('a[href*="artist"], a[href*="performer"]')) {
              rows.push(el);
              break;
            }
          }
        }
        return rows.filter((r, i, a) => a.indexOf(r) === i);
      },
      read: (row) => {
        const artistLinks = [...row.querySelectorAll('a[href*="artist"], a[href*="performer"]')];
        const artist = artistLinks.map(textOf).filter(Boolean).join(", ");
        const leaves = [...row.querySelectorAll("*")].filter(
          (e) => !e.children.length && textOf(e) && !artistLinks.some((a) => a.contains(e))
        );
        const durLeaf = leaves.find((e) => DURATION_RE.test(textOf(e)));
        const titleLeaf = leaves.find((e) => e !== durLeaf && !DURATION_RE.test(textOf(e)) && textOf(e) !== ",");
        return { artist, title: textOf(titleLeaf), duration: durLeaf ? toSeconds(textOf(durLeaf)) : null, id: null };
      },
    },
  ];

  // ---------- сбор ----------

  const sectionIds = new WeakMap();
  let nextSectionId = 1;
  const sectionOf = (row) => {
    const s = row.parentElement && row.parentElement.closest(SECTION);
    if (!s) return { id: 0, label: "" };
    if (!sectionIds.has(s)) {
      const head = s.querySelector('h1, h2, h3, [class*="header" i], [class*="Header"], [class*="title" i]');
      sectionIds.set(s, { id: nextSectionId++, label: head ? textOf(head).slice(0, 60) : "" });
    }
    return sectionIds.get(s);
  };

  const found = new Map(strategies.map((st) => [st, new Map()])); // стратегия → ключ → трек
  let skippedEmpty = 0;

  const harvest = () => {
    const regularFound = strategies.some((st) => !st.heuristic && found.get(st).size > 0);
    for (const st of strategies) {
      if (st.heuristic && regularFound) continue; // дорогая эвристика нужна, только если остальное не сработало
      let rows = st.rows();
      const kept = rows.filter((r) => !excluded(r));
      if (kept.length) rows = kept;
      const bucket = found.get(st);
      for (const row of rows) {
        let t;
        try {
          t = st.read(row);
        } catch (e) {
          t = null;
        }
        if (!t || !t.title) continue;
        if (!t.artist) {
          if (!st.heuristic) skippedEmpty++;
          continue;
        }
        const key = t.id ? `id:${t.id}` : `txt:${t.artist}|${t.title}|${t.duration || ""}`;
        if (!bucket.has(key)) bucket.set(key, { ...t, section: sectionOf(row), order: bucket.size });
      }
    }
  };

  const total = () => Math.max(...[...found.values()].map((m) => m.size));

  // Берём первую по приоритету стратегию, которая нашла не меньше 80% от лучшей.
  const choose = () => {
    const best = total();
    if (!best) return null;
    return strategies.find((st) => found.get(st).size >= best * 0.8);
  };

  const scrollables = () => {
    const main = document.scrollingElement || document.documentElement;
    const extra = [...document.querySelectorAll("div, main, section, ul")].filter((el) => {
      if (el.scrollHeight <= el.clientHeight + 50 || el.clientHeight < 150) return false;
      const oy = getComputedStyle(el).overflowY;
      return oy === "auto" || oy === "scroll";
    });
    return [main, ...extra];
  };

  const loaderVisible = () =>
    [...document.querySelectorAll('.CatalogBlock__autoListLoader, [class*="autoListLoader"], [class*="ListLoader"]')].some(
      (el) => el.offsetParent !== null && getComputedStyle(el).display !== "none"
    );

  let scrollTargets = [];
  const scrollStep = () => {
    let allAtEnd = true;
    for (const el of scrollTargets) {
      const before = el.scrollTop;
      const atEnd = before + el.clientHeight >= el.scrollHeight - 4;
      if (!atEnd) {
        allAtEnd = false;
        el.scrollTop = before + Math.max(200, el.clientHeight * 0.8);
      }
    }
    window.dispatchEvent(new Event("scroll"));
    return allAtEnd;
  };

  const toLine = (t) => {
    const artist = t.artist.replace(/\s*[—–]\s*/g, " - ");
    const title = t.title.replace(/\s*\|\s*$/, "");
    const dur = t.duration ? ` | ${fmt(t.duration)}` : "";
    return `${artist} — ${title}${dur}`;
  };

  const diagnostics = () => {
    const classes = new Set();
    document.querySelectorAll('[class*="udio"], [class*="rack"], [class*="usic"]').forEach((el) =>
      String(el.className).split(/\s+/).forEach((c) => /udio|rack|usic/.test(c) && classes.add(c))
    );
    return [...classes].slice(0, 40).join(" ");
  };

  const run = async () => {
    log("Начинаю: прокручиваю список до конца и собираю треки. Не закрывайте вкладку.");
    const started = Date.now();
    scrollTargets = scrollables();
    for (const el of scrollTargets) el.scrollTop = 0;
    await sleep(Math.min(500, OPT.stepMs));

    let idle = 0;
    let step = 0;
    while (Date.now() - started < OPT.maxMinutes * 60000) {
      const before = total();
      harvest();
      const atEnd = scrollStep();
      await sleep(OPT.stepMs);
      harvest();
      if (total() === before && atEnd) idle++;
      else idle = 0;
      // Пока виден индикатор подгрузки, ждём дольше, но не бесконечно.
      if (idle >= (loaderVisible() ? OPT.idleRounds * 4 : OPT.idleRounds)) break;
      if (++step % 10 === 0) {
        log(`Собрано треков: ${total()}…`);
        scrollTargets = scrollables();
      }
    }
    harvest();

    const chosen = choose();
    let tracks = chosen ? [...found.get(chosen).values()].sort((a, b) => a.order - b.order) : [];
    const warnings = [];
    if (!tracks.length) {
      const msg =
        "Не нашёл ни одной аудиозаписи на странице.\n\n" +
        "Проверьте:\n" +
        "• открыта страница со списком треков (vk.com → Музыка → «Моя музыка»), а не главная или плеер;\n" +
        "• список виден на экране, вы вошли в аккаунт;\n" +
        "• если вёрстка VK поменялась — пришлите строку «Классы на странице» из консоли, чтобы обновить скрипт.\n\n" +
        "Файл не создан.";
      console.error("[vk2sc] " + msg);
      console.error("[vk2sc] Классы на странице: " + (diagnostics() || "(ничего похожего на музыку)"));
      alert(msg);
      throw new Error("vk2sc: треки не найдены");
    }

    const sections = new Map();
    for (const t of tracks) sections.set(t.section.id, (sections.get(t.section.id) || 0) + 1);
    if (OPT.onlyBiggestSection && sections.size > 1) {
      const [bestId] = [...sections.entries()].sort((a, b) => b[1] - a[1])[0];
      const dropped = tracks.filter((t) => t.section.id !== bestId);
      tracks = tracks.filter((t) => t.section.id === bestId);
      const labels = [...new Set(dropped.map((t) => t.section.label).filter(Boolean))];
      warnings.push(
        `Взял только основной блок (${tracks.length} тр.), пропустил ${dropped.length} тр. из других блоков` +
          (labels.length ? `: ${labels.join("; ")}` : "") +
          ". Чтобы взять всё, запустите с window.VK2SC_OPTIONS = {onlyBiggestSection: false}."
      );
    }
    if (chosen.heuristic) {
      warnings.push("Треки найдены эвристикой — проверьте первые строки: не перепутаны ли исполнитель и название.");
    }
    if (skippedEmpty) warnings.push(`Пропущено строк без исполнителя: ${skippedEmpty}.`);
    const noDur = tracks.filter((t) => !t.duration).length;
    if (noDur) warnings.push(`Без длительности: ${noDur} тр. (сопоставление будет чуть менее точным).`);

    const lines = tracks.map(toLine);
    const header = `# Экспорт из VK ${new Date().toISOString().slice(0, 10)}, треков: ${lines.length}, способ: ${chosen.name}`;
    const text = [header, ...lines].join("\n") + "\n";

    window.vk2scResult = { count: lines.length, strategy: chosen.name, lines, text, warnings };
    warnings.forEach((w) => console.warn("[vk2sc] " + w));
    console.log(lines.slice(0, 10).join("\n") + (lines.length > 10 ? `\n… и ещё ${lines.length - 10}` : ""));

    if (OPT.download) {
      const url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
      const a = document.createElement("a");
      a.href = url;
      a.download = OPT.fileName;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
    }
    log(`Готово: ${lines.length} треков. Файл ${OPT.fileName} скачан (полный текст — в window.vk2scResult.text).`);
    return window.vk2scResult;
  };

  window.vk2scDone = run().catch((e) => {
    window.vk2scError = String(e && e.message ? e.message : e);
    if (!/треки не найдены/.test(window.vk2scError)) console.error("[vk2sc] Ошибка:", e);
    return null;
  });
})();
