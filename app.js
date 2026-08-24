(() => {
  "use strict";

  const elements = {
    channelLink: document.querySelector("#channel-link"),
    channelLogo: document.querySelector("#channel-logo"),
    channelHandle: document.querySelector("#channel-handle"),
    searchForm: document.querySelector("#search-form"),
    searchInput: document.querySelector("#search-input"),
    searchClear: document.querySelector("#search-clear"),
    viewEyebrow: document.querySelector("#view-eyebrow"),
    viewTitle: document.querySelector("#view-title"),
    resultCount: document.querySelector("#result-count"),
    sortControl: document.querySelector("#sort-control"),
    videoSort: document.querySelector("#video-sort"),
    loading: document.querySelector("#loading-state"),
    grid: document.querySelector("#video-grid"),
    empty: document.querySelector("#empty-state"),
    emptyReset: document.querySelector("#empty-reset"),
    playlistList: document.querySelector("#playlist-list"),
    playlistPanel: document.querySelector("#playlist-panel"),
    playlistToggle: document.querySelector("#playlist-toggle"),
    panelClose: document.querySelector("#panel-close"),
    drawerBackdrop: document.querySelector("#drawer-backdrop"),
    cardTemplate: document.querySelector("#video-card-template"),
  };

  const state = {
    catalog: null,
    videos: [],
    videosById: new Map(),
    playlists: [],
    playlistsById: new Map(),
    activePlaylistId: "all",
    query: "",
    normalizedQuery: "",
    videoSort: "newest",
    visibleVideos: [],
    playingVideoId: null,
  };

  const arabicDiacritics = /[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed]/g;

  function normalizeArabic(value) {
    return String(value || "")
      .toLocaleLowerCase("ar")
      .replace(arabicDiacritics, "")
      .replace(/ـ/g, "")
      .replace(/[إأآٱ]/g, "ا")
      .replace(/ى/g, "ي")
      .replace(/ؤ/g, "و")
      .replace(/ئ/g, "ي")
      .replace(/\s+/g, " ")
      .trim();
  }

  function formatDate(value) {
    if (!value) return "تاريخ غير متاح";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "تاريخ غير متاح";
    return new Intl.DateTimeFormat("ar", {
      year: "numeric",
      month: "long",
      day: "numeric",
    }).format(date);
  }

  function firstPlaylistFor(video) {
    const membershipIds = new Set((video.playlists || []).map((membership) => membership.id));
    return state.playlists.find((playlist) => membershipIds.has(playlist.id)) || null;
  }

  function playbackPlaylistFor(video) {
    if (!state.normalizedQuery && state.activePlaylistId !== "all") {
      const active = state.playlistsById.get(state.activePlaylistId);
      if (active && active.regularVideoIds.includes(video.id)) return active;
    }
    return firstPlaylistFor(video);
  }

  function youtubeUrl(video, playlist) {
    const url = new URL(video.url || `https://www.youtube.com/watch?v=${video.id}`);
    if (playlist) url.searchParams.set("list", playlist.id);
    return url.toString();
  }

  function embedUrl(video, playlist) {
    const base = video.embedUrl || `https://www.youtube-nocookie.com/embed/${video.id}`;
    const url = new URL(base);
    url.searchParams.set("autoplay", "1");
    url.searchParams.set("rel", "0");
    url.searchParams.set("modestbranding", "1");

    if (playlist) {
      const currentIndex = playlist.regularVideoIds.indexOf(video.id);
      const followingIds = currentIndex >= 0
        ? playlist.regularVideoIds.slice(currentIndex + 1)
        : playlist.regularVideoIds.filter((id) => id !== video.id);
      if (followingIds.length) url.searchParams.set("playlist", followingIds.join(","));
    }
    return url.toString();
  }

  function rankedSearch(query) {
    const normalizedQuery = normalizeArabic(query);
    if (!normalizedQuery) return state.videos.slice();
    const terms = [...new Set(normalizedQuery.split(" ").filter(Boolean))];

    return state.videos
      .map((video, originalIndex) => {
        const searchableText = normalizeArabic(`${video.title || ""} ${video.description || ""}`);
        const positions = terms.map((term) => searchableText.indexOf(term));
        if (positions.some((position) => position < 0)) return null;
        return {
          video,
          originalIndex,
          positionSum: positions.reduce((sum, position) => sum + position, 0),
        };
      })
      .filter(Boolean)
      .sort((left, right) =>
        left.positionSum - right.positionSum
        || left.originalIndex - right.originalIndex
      )
      .map((match) => match.video);
  }

  function sortVideosByDate(videos) {
    const direction = state.videoSort === "oldest" ? 1 : -1;
    return videos
      .map((video, originalIndex) => {
        const timestamp = Date.parse(video.publishedAt || video.uploadDate || "");
        return { video, originalIndex, timestamp: Number.isNaN(timestamp) ? null : timestamp };
      })
      .sort((left, right) => {
        if (left.timestamp === null && right.timestamp !== null) return 1;
        if (left.timestamp !== null && right.timestamp === null) return -1;
        if (left.timestamp !== null && right.timestamp !== null && left.timestamp !== right.timestamp) {
          return direction * (left.timestamp - right.timestamp);
        }
        return left.originalIndex - right.originalIndex;
      })
      .map((entry) => entry.video);
  }

  function currentVideos() {
    if (state.normalizedQuery) return rankedSearch(state.query);
    let videos;
    if (state.activePlaylistId === "all") {
      videos = state.videos.slice();
    } else {
      const playlist = state.playlistsById.get(state.activePlaylistId);
      videos = playlist
      ? playlist.regularVideoIds.map((id) => state.videosById.get(id)).filter(Boolean)
      : state.videos.slice();
    }
    return sortVideosByDate(videos);
  }

  function renderPlaylists() {
    elements.playlistList.replaceChildren();
    const entries = [
      {
        id: "all",
        title: "جميع الفيديوهات",
        count: state.videos.length,
        thumbnail: null,
      },
      ...state.playlists.map((playlist) => ({
        id: playlist.id,
        title: playlist.title,
        count: playlist.regularVideoIds.length,
        thumbnail: playlist.thumbnail,
      })),
    ];

    entries.forEach((entry) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "playlist-button";
      button.classList.toggle("is-active", !state.normalizedQuery && state.activePlaylistId === entry.id);
      button.dataset.playlistId = entry.id;
      button.setAttribute("aria-pressed", String(!state.normalizedQuery && state.activePlaylistId === entry.id));

      const visual = entry.thumbnail ? document.createElement("img") : document.createElement("span");
      visual.className = entry.thumbnail
        ? "playlist-button__thumb"
        : "playlist-button__thumb playlist-button__thumb--all";
      if (entry.thumbnail) {
        visual.src = entry.thumbnail;
        visual.alt = "";
        visual.loading = "lazy";
      } else {
        visual.textContent = "▶";
        visual.setAttribute("aria-hidden", "true");
      }

      const copy = document.createElement("span");
      copy.className = "playlist-button__copy";
      const title = document.createElement("span");
      title.className = "playlist-button__title";
      title.textContent = entry.title;
      const count = document.createElement("span");
      count.className = "playlist-button__count";
      count.textContent = `${entry.count.toLocaleString("ar")} فيديو`;
      copy.append(title, count);
      button.append(visual, copy);
      button.addEventListener("click", () => selectPlaylist(entry.id));
      elements.playlistList.append(button);
    });
  }

  function createMedia(video, playing) {
    if (playing) {
      const playlist = playbackPlaylistFor(video);
      const iframe = document.createElement("iframe");
      iframe.src = embedUrl(video, playlist);
      iframe.title = `تشغيل: ${video.title}`;
      iframe.allow = "autoplay; encrypted-media; picture-in-picture; fullscreen";
      iframe.allowFullscreen = true;
      iframe.referrerPolicy = "strict-origin-when-cross-origin";
      return iframe;
    }

    const button = document.createElement("button");
    button.type = "button";
    button.className = "video-card__play";
    button.setAttribute(
      "aria-label",
      `تشغيل ${video.title}${video.durationText ? `، المدة ${video.durationText}` : ""}`
    );

    const image = document.createElement("img");
    image.src = video.thumbnail;
    image.alt = "";
    image.loading = "lazy";
    image.decoding = "async";

    const playIcon = document.createElement("span");
    playIcon.className = "play-icon";
    playIcon.setAttribute("aria-hidden", "true");

    const duration = document.createElement("span");
    duration.className = "duration";
    duration.textContent = video.durationText || "";

    button.append(image, playIcon, duration);
    button.addEventListener("click", () => playVideo(video.id));
    return button;
  }

  function createVideoCard(video) {
    const card = elements.cardTemplate.content.firstElementChild.cloneNode(true);
    const playing = state.playingVideoId === video.id;
    card.dataset.videoId = video.id;
    card.classList.toggle("is-playing", playing);

    card.querySelector(".video-card__media").append(createMedia(video, playing));
    card.querySelector(".video-card__title").textContent = video.title;
    const date = card.querySelector("time");
    date.dateTime = video.publishedAt || video.uploadDate || "";
    date.textContent = formatDate(video.publishedAt || video.uploadDate);

    const playlist = playbackPlaylistFor(video);
    const youtubeLink = card.querySelector(".youtube-link");
    youtubeLink.href = youtubeUrl(video, playlist);
    youtubeLink.setAttribute(
      "aria-label",
      playlist ? `فتح الفيديو ضمن قائمة ${playlist.title} على يوتيوب` : "فتح الفيديو على يوتيوب"
    );
    return card;
  }

  function updateHeading() {
    elements.sortControl.hidden = Boolean(state.normalizedQuery);
    if (state.normalizedQuery) {
      elements.viewEyebrow.textContent = "نتائج البحث";
      elements.viewTitle.textContent = `نتائج «${state.query}»`;
    } else if (state.activePlaylistId === "all") {
      elements.viewEyebrow.textContent = "مكتبة الأكاديمية";
      elements.viewTitle.textContent = "جميع الفيديوهات";
    } else {
      const playlist = state.playlistsById.get(state.activePlaylistId);
      elements.viewEyebrow.textContent = "قائمة تشغيل";
      elements.viewTitle.textContent = playlist?.title || "جميع الفيديوهات";
    }
    elements.resultCount.textContent = `${state.visibleVideos.length.toLocaleString("ar")} فيديو`;
  }

  function renderVideos() {
    state.visibleVideos = currentVideos();
    if (!state.visibleVideos.some((video) => video.id === state.playingVideoId)) {
      state.playingVideoId = null;
    }
    elements.grid.replaceChildren(...state.visibleVideos.map(createVideoCard));
    elements.grid.hidden = state.visibleVideos.length === 0;
    elements.empty.hidden = state.visibleVideos.length !== 0;
    updateHeading();
    renderPlaylists();
  }

  function playVideo(videoId) {
    state.playingVideoId = state.playingVideoId === videoId ? null : videoId;
    renderVideos();
    if (state.playingVideoId) {
      requestAnimationFrame(() => {
        document.querySelector(`[data-video-id="${CSS.escape(videoId)}"]`)?.scrollIntoView({
          behavior: "smooth",
          block: "nearest",
        });
      });
    }
  }

  function selectPlaylist(playlistId) {
    state.activePlaylistId = playlistId;
    state.query = "";
    state.normalizedQuery = "";
    state.playingVideoId = null;
    elements.searchInput.value = "";
    elements.searchClear.hidden = true;
    renderVideos();
    closePlaylistDrawer();
  }

  function setSearch(value) {
    state.query = value.trim();
    state.normalizedQuery = normalizeArabic(state.query);
    state.playingVideoId = null;
    elements.searchClear.hidden = !state.query;
    renderVideos();
  }

  let drawerReturnFocus = null;
  const mobileDrawerQuery = window.matchMedia("(max-width: 820px)");

  function setPageInert(inert) {
    document.querySelector(".site-header").inert = inert;
    document.querySelector(".main-content").inert = inert;
  }

  function syncDrawerAccessibility() {
    const mobile = mobileDrawerQuery.matches;
    const open = mobile && elements.playlistPanel.classList.contains("is-open");
    if (mobile) {
      elements.playlistPanel.setAttribute("role", "dialog");
      elements.playlistPanel.setAttribute("aria-modal", "true");
      elements.playlistPanel.setAttribute("aria-hidden", String(!open));
      elements.playlistPanel.inert = !open;
    } else {
      elements.playlistPanel.removeAttribute("role");
      elements.playlistPanel.removeAttribute("aria-modal");
      elements.playlistPanel.removeAttribute("aria-hidden");
      elements.playlistPanel.inert = false;
      elements.playlistPanel.classList.remove("is-open");
      elements.drawerBackdrop.hidden = true;
      elements.playlistToggle.setAttribute("aria-expanded", "false");
      setPageInert(false);
      document.body.style.overflow = "";
    }
  }

  function openPlaylistDrawer() {
    if (!mobileDrawerQuery.matches) return;
    drawerReturnFocus = document.activeElement;
    elements.playlistPanel.classList.add("is-open");
    elements.playlistToggle.setAttribute("aria-expanded", "true");
    elements.drawerBackdrop.hidden = false;
    elements.playlistPanel.inert = false;
    elements.playlistPanel.setAttribute("aria-hidden", "false");
    setPageInert(true);
    document.body.style.overflow = "hidden";
    elements.panelClose.focus();
  }

  function closePlaylistDrawer() {
    elements.playlistPanel.classList.remove("is-open");
    elements.playlistToggle.setAttribute("aria-expanded", "false");
    elements.drawerBackdrop.hidden = true;
    if (mobileDrawerQuery.matches) {
      elements.playlistPanel.setAttribute("aria-hidden", "true");
      elements.playlistPanel.inert = true;
    }
    setPageInert(false);
    document.body.style.overflow = "";
    if (drawerReturnFocus instanceof HTMLElement) drawerReturnFocus.focus();
    drawerReturnFocus = null;
  }

  function trapDrawerFocus(event) {
    if (event.key !== "Tab" || !elements.playlistPanel.classList.contains("is-open")) return;
    const focusable = [...elements.playlistPanel.querySelectorAll(
      'button:not([disabled]), a[href], input:not([disabled]), [tabindex]:not([tabindex="-1"])'
    )].filter((element) => !element.hidden);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function wireEvents() {
    let searchTimer;
    elements.searchForm.addEventListener("submit", (event) => event.preventDefault());
    elements.searchInput.addEventListener("input", () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => setSearch(elements.searchInput.value), 90);
    });
    elements.searchClear.addEventListener("click", () => {
      elements.searchInput.value = "";
      setSearch("");
      elements.searchInput.focus();
    });
    elements.emptyReset.addEventListener("click", () => selectPlaylist("all"));
    elements.playlistToggle.addEventListener("click", openPlaylistDrawer);
    elements.panelClose.addEventListener("click", closePlaylistDrawer);
    elements.drawerBackdrop.addEventListener("click", closePlaylistDrawer);
    elements.videoSort.addEventListener("change", () => {
      state.videoSort = elements.videoSort.value;
      renderVideos();
    });
    elements.playlistPanel.addEventListener("keydown", trapDrawerFocus);
    mobileDrawerQuery.addEventListener("change", () => {
      closePlaylistDrawer();
      syncDrawerAccessibility();
    });
    window.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closePlaylistDrawer();
    });
    syncDrawerAccessibility();
  }

  async function initialize() {
    wireEvents();
    try {
      const response = await fetch("./data/youtube.json");
      if (!response.ok) throw new Error(`Catalog request failed: ${response.status}`);
      const catalog = await response.json();
      const regularVideos = catalog.videos.filter((video) => !video.isShort);
      const regularVideoIds = new Set(regularVideos.map((video) => video.id));
      const playlists = catalog.playlists
        .map((playlist) => ({
          ...playlist,
          regularVideoIds: playlist.videoIds.filter((id) => regularVideoIds.has(id)),
        }))
        .filter((playlist) => playlist.regularVideoIds.length > 0)
        .sort((left, right) => left.order - right.order);

      state.catalog = catalog;
      state.videos = regularVideos;
      state.videosById = new Map(regularVideos.map((video) => [video.id, video]));
      state.playlists = playlists;
      state.playlistsById = new Map(playlists.map((playlist) => [playlist.id, playlist]));

      elements.channelLink.href = catalog.channel.url;
      elements.channelHandle.textContent = catalog.channel.handle || "@arabicfutureacademy";
      elements.loading.hidden = true;
      renderVideos();

      window.__AFA_CATALOG_STATE__ = state;
      window.__AFA_TEST_API__ = { normalizeArabic, rankedSearch, selectPlaylist, playVideo };
    } catch (error) {
      console.error(error);
      elements.loading.innerHTML = "<strong>تعذّر تحميل مكتبة الفيديوهات.</strong><br>يرجى تحديث الصفحة والمحاولة مرة أخرى.";
    }
  }

  initialize();
})();
