const setupForm = document.getElementById("setup-form");
const setupError = document.getElementById("setup-error");
const setupButton = setupForm.querySelector("button[type='submit']");

setupForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  setupError.hidden = true;

  const password = document.getElementById("setup-password").value;
  const confirmation = document.getElementById("setup-password-confirm").value;
  if (password !== confirmation) {
    setupError.textContent = "Passwords do not match.";
    setupError.hidden = false;
    document.getElementById("setup-password-confirm").select();
    return;
  }

  setupButton.disabled = true;
  setupButton.textContent = "Creating administrator…";
  try {
    const response = await fetch("/api/setup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        display_name: document.getElementById("setup-display-name").value,
        username: document.getElementById("setup-username").value,
        password,
      }),
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.detail || "Administrator setup failed.");
    }
    window.location.replace("/");
  } catch (error) {
    setupError.textContent = error.message;
    setupError.hidden = false;
    setupButton.disabled = false;
    setupButton.textContent = "Create administrator";
  }
});
