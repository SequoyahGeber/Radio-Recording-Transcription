const loginForm = document.getElementById("login-form");
const loginError = document.getElementById("login-error");
const loginButton = loginForm.querySelector("button[type='submit']");

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  loginError.hidden = true;
  loginButton.disabled = true;
  loginButton.textContent = "Signing in…";

  try {
    const response = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: document.getElementById("login-username").value,
        password: document.getElementById("login-password").value,
      }),
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.detail || "Sign-in failed.");
    }
    window.location.replace("/");
  } catch (error) {
    loginError.textContent = error.message;
    loginError.hidden = false;
    document.getElementById("login-password").select();
  } finally {
    loginButton.disabled = false;
    loginButton.textContent = "Sign in securely";
  }
});
