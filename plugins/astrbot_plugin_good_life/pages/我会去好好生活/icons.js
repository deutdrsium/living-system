/* 内联线性图标。
 * 插件页运行在受限 iframe 里，不能从 CDN 取图标库；这里只收本页真正用到的几枚，
 * 统一 24 网格、1.8 描边、圆角端点，与陪伴小屋的线性图标同一种笔触。 */
"use strict";
window.GoodLifeIcons = (() => {
  const paths = {
    sprout: '<path d="M7 20h10"/><path d="M12 20v-8"/><path d="M12 12c0-4 2.5-6.5 7-7 0 4.5-2.5 7-7 7Z"/><path d="M12 14c0-3-2-5.2-6.5-5.5C5.5 12 7.6 14 12 14Z"/>',
    overview: '<rect x="3.5" y="3.5" width="7" height="8" rx="1.5"/><rect x="13.5" y="3.5" width="7" height="5" rx="1.5"/><rect x="13.5" y="11.5" width="7" height="9" rx="1.5"/><rect x="3.5" y="14.5" width="7" height="6" rx="1.5"/>',
    calendar: '<rect x="3.5" y="5" width="17" height="15.5" rx="2"/><path d="M3.5 10h17"/><path d="M8 3v4"/><path d="M16 3v4"/>',
    timetable: '<rect x="3.5" y="4.5" width="17" height="16" rx="2"/><path d="M3.5 9.5h17"/><path d="M9.5 9.5v11"/><path d="M15 9.5v11"/><path d="M3.5 15h17"/>',
    book:'<path d="M12 7c-1.8-1.6-4.5-2.5-8.5-2.5v14c4 0 6.7.9 8.5 2.5 1.8-1.6 4.5-2.5 8.5-2.5v-14C16.5 4.5 13.8 5.4 12 7Z"/><path d="M12 7v14"/>',
    compass: '<circle cx="12" cy="12" r="8.5"/><path d="m15.5 8.5-2 5-5 2 2-5Z"/>',
    sliders: '<path d="M4 7h9"/><path d="M17 7h3"/><circle cx="15" cy="7" r="2"/><path d="M4 17h3"/><path d="M11 17h9"/><circle cx="9" cy="17" r="2"/>',
    refresh: '<path d="M20 11a8 8 0 0 0-14.3-4.5L4 8.5"/><path d="M4 4v4.5h4.5"/><path d="M4 13a8 8 0 0 0 14.3 4.5L20 15.5"/><path d="M20 20v-4.5h-4.5"/>',
    menu: '<path d="M4 7h16"/><path d="M4 12h16"/><path d="M4 17h16"/>',
    close: '<path d="m6 6 12 12"/><path d="M18 6 6 18"/>',
    clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
    house: '<path d="M4 10.5 12 4l8 6.5"/><path d="M6 9v10.5h12V9"/><path d="M10 19.5v-5h4v5"/>',
    external: '<path d="M14 4h6v6"/><path d="M20 4 11 13"/><path d="M18 14v5a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 19V7.5A1.5 1.5 0 0 1 5.5 6H10"/>',
    activity: '<path d="M3.5 12h4l2.5-6 4 12 2.5-6h4"/>',
    pause: '<rect x="7" y="5.5" width="3" height="13" rx="1"/><rect x="14" y="5.5" width="3" height="13" rx="1"/>',
    energy: '<rect x="3" y="7.5" width="16" height="9" rx="2"/><path d="M21 11v2"/><path d="M6.5 10.5v3"/>',
    "energy-full": '<rect x="3" y="7.5" width="16" height="9" rx="2"/><path d="M21 11v2"/><path d="M6.5 10.5v3"/><path d="M10 10.5v3"/><path d="M13.5 10.5v3"/>',
    "energy-low": '<rect x="3" y="7.5" width="16" height="9" rx="2"/><path d="M21 11v2"/><path d="M6.5 10.5v3"/>',
    health: '<path d="M12 19.5s-7.5-4.4-7.5-10A4.2 4.2 0 0 1 12 6.8a4.2 4.2 0 0 1 7.5 2.7c0 5.6-7.5 10-7.5 10Z"/><path d="M7.5 12h2.5l1.2-2 1.8 4 1.2-2H16.5"/>',
    hunger: '<path d="M6.5 3.5v6a2 2 0 0 0 4 0v-6"/><path d="M8.5 3.5v17"/><path d="M17.5 20.5v-17c-2.2 1.2-3.5 3.6-3.5 7v3h3.5"/>',
    mood: '<circle cx="12" cy="12" r="8.5"/><path d="M8.8 14.2c.8 1 1.9 1.6 3.2 1.6s2.4-.6 3.2-1.6"/><path d="M9 9.8h.01"/><path d="M15 9.8h.01"/>',
    sleep: '<path d="M19.5 14.5A7.5 7.5 0 1 1 9.5 4.5a6 6 0 0 0 10 10Z"/>',
    sun: '<circle cx="12" cy="12" r="3.8"/><path d="M12 3v1.8"/><path d="M12 19.2V21"/><path d="m5.6 5.6 1.3 1.3"/><path d="m17.1 17.1 1.3 1.3"/><path d="M3 12h1.8"/><path d="M19.2 12H21"/><path d="m5.6 18.4 1.3-1.3"/><path d="m17.1 6.9 1.3-1.3"/>',
    cycle: '<rect x="3.5" y="5" width="17" height="15.5" rx="2"/><path d="M3.5 10h17"/><path d="M8 3v4"/><path d="M16 3v4"/><path d="M12 18s-3-1.7-3-3.6a1.5 1.5 0 0 1 3-.6 1.5 1.5 0 0 1 3 .6C15 16.3 12 18 12 18Z"/>',
    busy: '<rect x="3.5" y="7.5" width="17" height="12" rx="2"/><path d="M9 7.5V5.5A1.5 1.5 0 0 1 10.5 4h3A1.5 1.5 0 0 1 15 5.5v2"/><path d="M3.5 12.5h17"/>',
    location: '<path d="M12 20.5s-6.5-5.6-6.5-10.5a6.5 6.5 0 0 1 13 0c0 4.9-6.5 10.5-6.5 10.5Z"/><circle cx="12" cy="10" r="2.3"/>',
    chevron: '<path d="m9 6 6 6-6 6"/>',
    "chevron-left": '<path d="m15 6-6 6 6 6"/>',
    plus: '<path d="M12 5v14"/><path d="M5 12h14"/>',
    repeat: '<path d="M17 3.5 20 6.5l-3 3"/><path d="M4 11.5v-1a4 4 0 0 1 4-4h12"/><path d="m7 20.5-3-3 3-3"/><path d="M20 12.5v1a4 4 0 0 1-4 4H4"/>',
    bell: '<path d="M6 16.5V11a6 6 0 0 1 12 0v5.5l1.5 2h-15Z"/><path d="M10 20.5a2 2 0 0 0 4 0"/>',
    edit: '<path d="M4 20h4L19 9a2.1 2.1 0 0 0-3-3L5 17Z"/><path d="m14.5 7.5 3 3"/>',
    wallet: '<path d="M4 7.5A2.5 2.5 0 0 1 6.5 5H18v3"/><path d="M4 7.5v10A2.5 2.5 0 0 0 6.5 20h13v-12h-13A2.5 2.5 0 0 1 4 7.5Z"/><path d="M16 14h.01"/>',
    "arrow-in": '<path d="M12 4.5v13"/><path d="m6.5 12 5.5 5.5 5.5-5.5"/><path d="M5 20.5h14"/>',
    "arrow-out": '<path d="M12 19.5v-13"/><path d="m6.5 12 5.5-5.5 5.5 5.5"/><path d="M5 3.5h14"/>',
    alert: '<path d="M12 4 2.8 19.5h18.4Z"/><path d="M12 10v4.5"/><path d="M12 17.2h.01"/>',
  };
  function icon(name, cls = "") {
    const body = paths[name] || paths.activity;
    return `<svg class="gl-icon${cls ? " " + cls : ""}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${body}</svg>`;
  }
  /* 静态标记里的 <i data-icon="..."> 在首屏统一换成 SVG。 */
  function hydrate(root = document) {
    root.querySelectorAll("i[data-icon]").forEach(item => { item.outerHTML = icon(item.dataset.icon, item.className); });
  }
  return {icon, hydrate, names:Object.keys(paths)};
})();
