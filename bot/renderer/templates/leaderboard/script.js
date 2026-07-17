window.initializeUI = function() {
  const data = window.renderData;
  if (!data) return;

  // Set Header Info
  document.getElementById("season-type").textContent = `${data.timeframe_name} XP RANKINGS`;
  document.getElementById("server-name").textContent = data.server_name;
  document.getElementById("total-players").textContent = `${data.total_players.toLocaleString()} Total Players`;
  document.getElementById("last-updated").textContent = `Generated: ${data.generated_at}`;
  document.getElementById("page-number").textContent = `Page ${data.current_page} of ${data.total_pages}`;

  // Set Caller Card Standing Info
  const caller = data.caller;
  document.getElementById("caller-name").textContent = caller.username;
  document.getElementById("caller-path-name").textContent = caller.path_name || "No Master Path selected";
  document.getElementById("caller-rank").textContent = caller.rank > 0 ? `#${caller.rank}` : "Unranked";
  document.getElementById("caller-score").textContent = `${caller.score.toLocaleString()} XP`;
  document.getElementById("caller-level-badge").textContent = `LV ${caller.level}`;
  
  // Set caller avatar (with default fallback check)
  const callerAvatarEl = document.getElementById("caller-avatar");
  if (caller.avatar) {
    callerAvatarEl.src = caller.avatar;
  } else {
    callerAvatarEl.src = "https://cdn.discordapp.com/embed/avatars/0.png"; // Discord default
  }

  // Calculate Caller XP percentage
  const progressPct = caller.next_level_xp > 0 ? (caller.current_level_xp / caller.next_level_xp) * 100 : 0;
  document.getElementById("caller-xp-pct").textContent = `${Math.min(100, Math.round(progressPct))}%`;
  document.getElementById("caller-xp-bar").style.width = `${Math.min(100, progressPct)}%`;
  document.getElementById("caller-current-xp").textContent = caller.current_level_xp.toLocaleString();
  document.getElementById("caller-next-xp").textContent = caller.next_level_xp.toLocaleString();

  // Populate Table Rows
  const container = document.getElementById("rows-container");
  container.innerHTML = ""; // Clear placeholders

  // We loop up to 10 rows (standard page limit)
  for (let i = 0; i < 10; i++) {
    const rowData = data.leaderboard[i];
    const rowEl = document.createElement("div");
    
    // Default column layout class matching index.html headers
    rowEl.className = "grid grid-cols-[70px_70px_1fr_100px_160px] items-center px-6 py-1.5 rounded-xl border border-transparent font-heading transition-all";
    
    if (rowData) {
      const isCaller = rowData.user_id === caller.user_id;
      const rank = rowData.rank;
      
      // Apply rank highlighting styles
      if (isCaller) {
        rowEl.classList.add("row-rank-caller");
      } else if (rank === 1) {
        rowEl.classList.add("row-rank-1");
      } else if (rank === 2) {
        rowEl.classList.add("row-rank-2");
      } else if (rank === 3) {
        rowEl.classList.add("row-rank-3");
      } else {
        rowEl.className += " bg-[rgba(22,28,38,0.45)] border-white/[0.03]";
      }

      // Rank Column (with trophy medals for top 3)
      let rankHTML = "";
      if (rank === 1) {
        rankHTML = `<span class="text-2xl">🥇</span>`;
      } else if (rank === 2) {
        rankHTML = `<span class="text-2xl">🥈</span>`;
      } else if (rank === 3) {
        rankHTML = `<span class="text-2xl">🥉</span>`;
      } else {
        const starPrefix = isCaller ? `<span class="text-pink-500 mr-1 font-heading">★</span>` : "";
        rankHTML = `<span class="text-base font-mono font-bold text-slate-400">${starPrefix}${rank}</span>`;
      }

      // Avatar Column
      const avatarSrc = rowData.avatar ? rowData.avatar : "https://cdn.discordapp.com/embed/avatars/0.png";

      // Build row structure
      rowEl.innerHTML = `
        <div class="flex justify-center">${rankHTML}</div>
        <div class="flex justify-center">
          <img class="w-10 h-10 rounded-full object-cover border border-white/10" src="${avatarSrc}" alt="">
        </div>
        <div class="font-bold text-base truncate pr-4 text-slate-100 flex items-center gap-2">
          ${rowData.username}
          ${isCaller ? `<span class="text-[10px] bg-pink-500/20 text-pink-400 px-1.5 py-0.5 rounded font-mono font-semibold uppercase tracking-wider">YOU</span>` : ""}
        </div>
        <div class="text-center font-mono font-semibold text-slate-200">Lv ${rowData.level}</div>
        <div class="text-right font-mono font-bold text-slate-100">${rowData.score.toLocaleString()} XP</div>
      `;
    } else {
      // Empty row placeholder to preserve perfect vertical height alignment
      rowEl.innerHTML = `
        <div class="col-span-5 h-[42px]"></div>
      `;
      rowEl.classList.add("opacity-0");
    }

    container.appendChild(rowEl);
  }

  // Refresh lucide icons
  if (window.lucide) {
    lucide.createIcons();
  }
};
