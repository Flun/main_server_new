const API_ROOT = "http://127.0.0.1:8999";
const syncButton = document.getElementById("sync");
const statusBox = document.getElementById("status");

async function syncCookies() {
  syncButton.disabled = true;
  statusBox.textContent = "Chrome에서 Instagram 쿠키 읽는 중…";
  try {
    const cookies = await chrome.cookies.getAll({ domain: "instagram.com" });
    if (!cookies.length) throw new Error("Instagram 쿠키가 없습니다. Chrome에서 Instagram에 로그인했는지 확인하세요.");
    const response = await fetch(`${API_ROOT}/api/instagram-cookie-sync`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cookies })
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "동기화 실패");
    statusBox.textContent = `${result.count}개 동기화 완료${result.has_session ? " · 로그인 세션 확인" : "\n주의: sessionid가 없어 로그인이 필요할 수 있습니다."}`;
  } catch (error) {
    statusBox.textContent = `실패: ${error.message}`;
  } finally {
    syncButton.disabled = false;
  }
}

syncButton.addEventListener("click", syncCookies);
