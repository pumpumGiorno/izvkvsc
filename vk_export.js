/*
 * Экспорт списка аудиозаписей VK в tracks.txt — запускать в консоли браузера
 * на странице со своей музыкой (vk.com/audios… или vk.com/music/…). Подробности — в README.
 *
 * Скрипт только читает страницу: ничего не отправляет по сети и ничего не
 * меняет в аккаунте. Он прокручивает список до конца, собирает треки и
 * скачивает файл «Исполнитель — Название | м:сс», по одному треку в строке.
 *
 * Вёрстка VK меняется, поэтому треки ищутся несколькими способами по очереди:
 *   1) новая VK Музыка: data-testid="MusicTrackRow" и его _Title/_Authors/_Duration;
 *   2) классы audio_row__performers / audio_row__title_inner;
 *   3) атрибут data-audio со служебными данными трека;
 *   4) классы, похожие на AudioRow / TrackRow с title / artist / duration внутри;
 *   5) эвристика: строка с длительностью «м:сс» и ссылкой на исполнителя.
 * Бейджи расширений (битрейт «320», «~128», размер файла, кнопки) в название не попадают.
 * Если треков нет или названия не читаются — понятная ошибка, а не файл с мусором.
 */
(() => {
  const OPT = Object.assign(
    {
      stepMs: 800, // пауза после каждого шага прокрутки
      idleRounds: 6, // столько шагов подряд без новых треков внизу страницы = конец списка
      maxMinutes: 20, // страховка от бесконечной прокрутки
      onlyBiggestSection: true, // брать только самый большой блок треков на странице
      maxBadShare: 0.2, // если плохих названий больше этой доли — файл не сохраняется
      download: true, // скачать tracks.txt
      fileName: "tracks.txt",
    },
    window.VK2SC_OPTIONS || {}
  );

  const DURATION_RE = /^(?:\d{1,2}:)?\d{1,2}:\d{2}$/;
  // Бейджи расширений и служебные подписи: битрейт, качество, размер файла.
  // Голые числа считаем битрейтом, только если это стандартное значение: песня «22» остаётся песней.
  const BADGE_TEXT_RE = new RegExp(
    "^(?:~\\s?\\d{2,4}(?:\\s*kbps)?" +
      "|\\d{2,4}\\s*(?:kbps|kbit/s|kb/s|кбит/с|кбит)" +
      "|(?:32|40|48|56|64|80|96|112|128|160|192|224|256|320)" +
      "|hq|lq|hd|hi-?res|flac|mp3|aac|lossless" +
      "|\\d+(?:[.,]\\d+)?\\s*(?:кб|мб|гб|kb|mb|gb))$",
    "i"
  );
  const TRAILING_BADGE_RE = /\s+(?:~\s?\d{2,4}(?:\s*kbps)?|\d{2,4}\s*(?:kbps|kbit\/s|кбит\/с))\s*$/i;
  const BADGE_CLASS_RE = /bitrate|kbps|quality|badge|filesize|__size|download/i;
  const HIDDEN_SEL = '[class*="VisuallyHidden"], [class*="visually-hidden"], [hidden]';

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

  // ---------- текст без бейджей ----------

  // Бейдж по классу или скрытая подпись («Открыть сниппет») — пропускаем всегда.
  const isNoise = (el) =>
    el.matches(HIDDEN_SEL) ||
    /display:\s*none/i.test(el.getAttribute("style") || "") ||
    BADGE_CLASS_RE.test(el.getAttribute("class") || "");
  // Бейдж по тексту («320», «HQ») — только внутри названия: у группы «112» имя из цифр.
  const isTextBadge = (el) => {
    const t = clean(el.textContent);
    return t.length > 0 && t.length <= 16 && BADGE_TEXT_RE.test(t);
  };
  const BLOCK_RE = /^(div|p|li|ul|ol|h[1-6]|section|article|header|footer|br|tr|td)$/i;

  // Видимый текст узла без бейджей, скрытых подписей и иконок.
  const visibleText = (node, textBadges = false) => {
    if (!node) return "";
    if (node.nodeType === Node.TEXT_NODE) return node.nodeValue;
    if (node.nodeType !== Node.ELEMENT_NODE) return "";
    if (/^(svg|img|script|style|button)$/i.test(node.tagName) || isNoise(node)) return "";
    if (textBadges && isTextBadge(node)) return "";
    const inner = [...node.childNodes].map((n) => visibleText(n, textBadges)).join("");
    return BLOCK_RE.test(node.tagName) ? ` ${inner} ` : inner; // как innerText: блоки не слипаются
  };
  const textOf = (el) => clean(visibleText(el));
  const segmentOf = (node, strict) => {
    const t = clean(visibleText(node, strict)).replace(TRAILING_BADGE_RE, "");
    return t && !(strict && BADGE_TEXT_RE.test(t)) ? t : "";
  };

  // Куски названия по порядку: «Have You Ever Been Mellow», «Flamman & Abraxas Radio Mix».
  const titlePartsOf = (el, strict) => {
    let node = el;
    for (let depth = 0; depth < 6; depth++) {
      const parts = [...node.childNodes].map((n) => segmentOf(n, strict)).filter(Boolean);
      const kids = [...node.children].filter((c) => segmentOf(c, strict));
      // Спускаемся сквозь обёртки: <a><div><span>Название</span><span>Версия</span></div></a>.
      if (parts.length === 1 && kids.length === 1 && kids[0].childNodes.length > 1 && segmentOf(kids[0], strict) === parts[0]) {
        node = kids[0];
        continue;
      }
      return parts;
    }
    return [segmentOf(node, strict)].filter(Boolean);
  };
  // Сначала без бейджей-текстов («Fading» + «~128»). Если не осталось ничего, значит само название
  // из цифр («128», «HQ»): берём как есть, дальше его оценят проверка качества и vk2sc.
  const titleParts = (el) => {
    const strict = titlePartsOf(el, true);
    return strict.length ? strict : titlePartsOf(el, false);
  };

  // Основное название + подзаголовок (название версии) в скобках.
  const composeTitle = (parts) => {
    if (!parts.length) return "";
    const [main, ...rest] = parts;
    const sub = rest.filter((p) => p.length > 1).join(" ").replace(/^\((.*)\)$/, "$1").trim();
    if (!sub || main.toLowerCase().includes(sub.toLowerCase())) return main;
    return `${main} (${sub})`;
  };
  const titleOf = (el) => (el ? composeTitle(titleParts(el)) : "");

  const isBadTitle = (title) => !title || /^\d+$/.test(title) || BADGE_TEXT_RE.test(title);

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

  const outermost = (els, sel) => els.filter((el) => !(el.parentElement && el.parentElement.closest(sel)));

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
  const EXCLUDE =
    '#top_audio_layer_place, .top_audio_layer, .audio_layer_container, .audio_page_player, [class*="top_audio_player"],' +
    ' [data-testid="TopAudioPlayer"], [data-testid="AudioPage_PlayerBlock"]';
  const SECTION = '.CatalogBlock, [class*="CatalogBlock__root"], [class*="CatalogBlock_"], section, [class*="Section"], .audio_page__audio_rows_list, ._audio_page__audio_rows_list';

  const excluded = (el) => !!el.closest(EXCLUDE);
  const ROW_LIKE =
    '[class*="AudioRow"], [class*="audio-row"], [class*="TrackRow"], [class*="track-row"], [class*="MusicTrack"], [data-testid*="audio" i], [data-testid*="track" i]';
  const TRACK_ROW = '[data-testid*="TrackRow" i]';

  // Сколько длительностей «м:сс» внутри каждого элемента: строка трека содержит ровно одну.
  const durationCounts = () => {
    const counts = new Map();
    for (const leaf of durationLeaves(document.body)) {
      for (let el = leaf.parentElement; el; el = el.parentElement) counts.set(el, (counts.get(el) || 0) + 1);
    }
    return counts;
  };

  // ---------- стратегии ----------

  const strategies = [
    {
      name: "VK Музыка (data-testid)",
      rows: () => [...document.querySelectorAll(TRACK_ROW)].filter((el) => !el.getAttribute("data-testid").includes("_")),
      read: (row) => {
        const part = (suffix) =>
          [...row.querySelectorAll(TRACK_ROW)].filter((el) => el.getAttribute("data-testid").endsWith(suffix));
        const authors = outermost(part("_Authors"), '[data-testid$="_Authors"]');
        const holder = row.closest("[data-audio-id]");
        return {
          artist: authors.map(textOf).filter(Boolean).join(", "),
          title: titleOf(part("_Title")[0]),
          duration: toSeconds(textOf(part("_Duration")[0])),
          id: holder ? holder.getAttribute("data-audio-id") : null,
        };
      },
    },
    {
      name: "классы audio_row__*",
      rows: () =>
        [...document.querySelectorAll(".audio_row__performers")]
          .map((p) => p.closest(".audio_row, ._audio_row, [data-audio], [data-full-id]") || p.parentElement.parentElement)
          .filter((r, i, a) => r && a.indexOf(r) === i),
      read: (row) => {
        const data = parseDataAudio(row.closest("[data-audio]") || row);
        return {
          artist: textOf(row.querySelector(".audio_row__performers")),
          title: titleOf(row.querySelector(".audio_row__title") || row.querySelector(".audio_row__title_inner")),
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
        // Строка — самый внешний «AudioRow-подобный» элемент, где не больше одной длительности
        // (у недоступных треков её может не быть); контейнер всего списка
        // (AudioCatalog_BlockMusicAudiosList, несколько длительностей) строкой не считается.
        const counts = durationCounts();
        const n = (el) => counts.get(el) || 0;
        return [...document.querySelectorAll(ROW_LIKE)].filter((el) => {
          if (n(el) > 1) return false;
          const up = el.parentElement && el.parentElement.closest(ROW_LIKE);
          return !up || n(up) > 1;
        }).filter((r) => r.querySelector('[data-testid*="Title"], [class*="itle"]'));
      },
      read: (row) => {
        const first = (sel) => [...row.querySelectorAll(sel)].find((e) => textOf(e));
        const title = titleOf(
          first('[data-testid*="Title"], [class*="Title"]:not([class*="Subtitle"]), [class*="title"]:not([class*="subtitle"])')
        );
        const artist = textOf(
          first('[data-testid*="uthor"], [data-testid*="rtist"], [class*="erformer"], [class*="rtist"], [class*="uthor"], [class*="Subtitle"], [class*="subtitle"]')
        );
        const leaf = durationLeaves(row)[0];
        const data = parseDataAudio(row);
        return {
          artist,
          title,
          duration: leaf ? toSeconds(leaf.textContent) : null,
          id: (data && data.id) || row.getAttribute("data-id"),
        };
      },
    },
    {
      name: "эвристика по длительности",
      heuristic: true,
      rows: () => {
        const rows = [];
        for (const leaf of durationLeaves(document.body)) {
          let el = leaf.parentElement;
          for (let depth = 0; el && depth < 8; depth++, el = el.parentElement) {
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
        const artistLinks = outermost(
          [...row.querySelectorAll('a[href*="artist"], a[href*="performer"]')],
          'a[href*="artist"], a[href*="performer"]'
        );
        const noisy = (e) => {
          for (let x = e; x && x !== row; x = x.parentElement) if (isNoise(x)) return true;
          return false;
        };
        const ownText = (e) =>
          clean([...e.childNodes].filter((n) => n.nodeType === Node.TEXT_NODE).map((n) => n.nodeValue).join(""));
        // Название — первый элемент со своим текстом (не лист: у названия с версией внутри есть <span>).
        const owner = [...row.querySelectorAll("*")].find((e) => {
          const own = ownText(e);
          return own && own !== "," && !DURATION_RE.test(own) && !BADGE_TEXT_RE.test(own) &&
            !artistLinks.some((a) => a.contains(e)) && !noisy(e);
        });
        const leaf = durationLeaves(row)[0];
        return {
          artist: artistLinks.map(textOf).filter(Boolean).join(", "),
          title: titleOf(owner),
          duration: leaf ? toSeconds(leaf.textContent) : null,
          id: null,
        };
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
  const noArtist = new Set();

  const goodCount = (st) => {
    let n = 0;
    for (const t of found.get(st).values()) if (!t.bad) n++;
    return n;
  };

  const harvest = () => {
    strategies.forEach((st, i) => {
      // Дорогие запасные способы не нужны, если способ выше по списку уже уверенно нашёл треки.
      const earlier = strategies.slice(0, i).filter((s) => !s.heuristic);
      if (earlier.some((s) => goodCount(s) >= 20)) return;
      if (st.heuristic && earlier.some((s) => goodCount(s) > 0)) return;
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
        if (!t) continue;
        if (!t.artist) {
          if (t.title && !st.heuristic) noArtist.add(`${t.title}|${t.duration || ""}`);
          continue;
        }
        t.title = t.title || "";
        t.bad = isBadTitle(t.title);
        const key = t.id ? `id:${t.id}` : `txt:${t.artist}|${t.title}|${t.duration || ""}`;
        const prev = bucket.get(key);
        if (!prev) bucket.set(key, { ...t, section: sectionOf(row), order: bucket.size });
        // Строка могла попасть в сбор раньше, чем VK дорисовал название, — обновляем, порядок тот же.
        else if (prev.bad && !t.bad) bucket.set(key, { ...t, section: prev.section, order: prev.order });
      }
    });
  };

  const total = () => Math.max(...[...found.values()].map((m) => m.size));

  // Берём первую по приоритету стратегию, у которой хороших названий не меньше 80% от лучшей.
  // Если хороших нет ни у кого — ту, что нашла больше всего строк (дальше сработает проверка качества).
  const choose = () => {
    const bestGood = Math.max(...strategies.map(goodCount));
    if (bestGood > 0) return strategies.find((st) => goodCount(st) >= bestGood * 0.8);
    const most = total();
    return most ? strategies.find((st) => found.get(st).size === most) : null;
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
  const describe = (t) => `${t.artist} — ${t.title || "(без названия)"}${t.duration ? ` [${fmt(t.duration)}]` : ""}`;

  const diagnostics = () => {
    const classes = new Set();
    document.querySelectorAll('[class*="udio"], [class*="rack"], [class*="usic"]').forEach((el) =>
      String(el.className).split(/\s+/).forEach((c) => /udio|rack|usic/.test(c) && classes.add(c))
    );
    return [...classes].slice(0, 40).join(" ");
  };

  const fail = (msg, code) => {
    console.error("[vk2sc] " + msg);
    alert(msg);
    throw new Error(`vk2sc: ${code}`);
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
      console.error("[vk2sc] Классы на странице: " + (diagnostics() || "(ничего похожего на музыку)"));
      fail(
        "Не нашёл ни одной аудиозаписи на странице.\n\n" +
          "Проверьте:\n" +
          "• открыта страница со списком треков (vk.com → Музыка → «Моя музыка»), а не главная или плеер;\n" +
          "• список виден на экране, вы вошли в аккаунт;\n" +
          "• если вёрстка VK поменялась — запустите vk_diagnose.js и пришлите вывод (или строку «Классы на странице» из консоли).\n\n" +
          "Файл не создан.",
        "треки не найдены"
      );
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

    // Проверка качества: пустые, числовые и «битрейтные» названия — признак того, что вёрстку
    // читает не тот элемент (чаще всего из-за расширения, которое вставляет бейджи в строки).
    const bad = tracks.filter((t) => t.bad);
    const share = bad.length / tracks.length;
    if (share > OPT.maxBadShare) {
      console.error("[vk2sc] Примеры: \n" + bad.slice(0, 10).map(describe).join("\n"));
      fail(
        `Похоже, названия треков считываются неправильно: у ${bad.length} из ${tracks.length} ` +
          `(${Math.round(share * 100)}%) название пустое, состоит из цифр или похоже на битрейт («320», «~128»).\n\n` +
          "Скорее всего, мешает расширение браузера, которое добавляет в список треков бейджи " +
          "(битрейт, размер файла, кнопки скачивания). Отключите его, обновите страницу и запустите скрипт снова.\n" +
          "Если не поможет — запустите vk_diagnose.js и пришлите его вывод.\n\n" +
          "Файл не создан.",
        "названия не распознаны"
      );
    }
    // Пустая копия строки, собранная до отрисовки названия (если у строки нет id).
    const titled = new Set(tracks.filter((t) => t.title).map((t) => `${t.artist}|${t.duration || ""}`));
    tracks = tracks.filter((t) => t.title || t.id || !titled.has(`${t.artist}|${t.duration || ""}`));
    const untitled = tracks.filter((t) => !t.title);
    if (untitled.length) {
      warnings.push(
        `Пропущено треков без названия: ${untitled.length}. Добавьте их в tracks.txt вручную: ` +
          untitled.slice(0, 10).map(describe).join("; ") + (untitled.length > 10 ? "; …" : "")
      );
      tracks = tracks.filter((t) => t.title);
    }
    const odd = tracks.filter((t) => t.bad);
    if (odd.length) warnings.push(`Проверьте названия из одних цифр: ${odd.slice(0, 10).map(describe).join("; ")}.`);
    if (chosen.heuristic) {
      warnings.push("Треки найдены эвристикой — проверьте первые строки: не перепутаны ли исполнитель и название.");
    }
    if (noArtist.size) warnings.push(`Пропущено строк без исполнителя: ${noArtist.size}.`);
    const noDur = tracks.filter((t) => !t.duration).length;
    if (noDur) warnings.push(`Без длительности: ${noDur} тр. (сопоставление будет чуть менее точным).`);

    const lines = tracks.map(toLine);
    const header = `# Экспорт из VK ${new Date().toISOString().slice(0, 10)}, треков: ${lines.length}, способ: ${chosen.name}`;
    const text = [header, ...lines].join("\n") + "\n";

    window.vk2scResult = { count: lines.length, strategy: chosen.name, lines, text, warnings, badShare: share };
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
    if (!/^vk2sc: /.test(window.vk2scError)) console.error("[vk2sc] Ошибка:", e);
    return null;
  });
})();
