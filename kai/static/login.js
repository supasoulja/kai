/* login.js — login & registration logic (extracted from inline script) */
(function () {
  "use strict";

  const loginPanel    = document.getElementById('login-panel');
  const registerPanel = document.getElementById('register-panel');
  const showRegister  = document.getElementById('show-register');
  const showLogin     = document.getElementById('show-login');
  const loginUserSel  = document.getElementById('login-user');
  const loginPinIn    = document.getElementById('login-pin');
  const loginError    = document.getElementById('login-error');
  const loginBtn      = document.getElementById('login-btn');
  const loginForm     = document.getElementById('login-form');
  const regNameIn     = document.getElementById('reg-name');
  const regPinIn      = document.getElementById('reg-pin');
  const registerError = document.getElementById('register-error');
  const registerBtn   = document.getElementById('register-btn');
  const registerForm  = document.getElementById('register-form');

  // --- Toggle panels ---
  showRegister.addEventListener('click', () => {
    loginPanel.classList.add('hidden');
    registerPanel.classList.remove('hidden');
    loginError.textContent = '';
    regNameIn.focus();
  });

  showLogin.addEventListener('click', () => {
    registerPanel.classList.add('hidden');
    loginPanel.classList.remove('hidden');
    registerError.textContent = '';
    loginUserSel.focus();
  });

  // --- Populate user dropdown ---
  async function loadUsers() {
    try {
      const res = await fetch('/users');
      if (!res.ok) throw new Error('Failed to load users');
      const data = await res.json();
      const names = Array.isArray(data.names) ? data.names : [];
      loginUserSel.innerHTML = '';
      if (names.length === 0) {
        const opt = document.createElement('option');
        opt.value = '';
        opt.disabled = true;
        opt.selected = true;
        opt.textContent = 'No users yet \u2014 register first';
        loginUserSel.appendChild(opt);
      } else {
        const placeholder = document.createElement('option');
        placeholder.value = '';
        placeholder.disabled = true;
        placeholder.selected = true;
        placeholder.textContent = 'Select a user\u2026';
        loginUserSel.appendChild(placeholder);
        names.forEach(name => {
          const opt = document.createElement('option');
          opt.value = name;
          opt.textContent = name;
          loginUserSel.appendChild(opt);
        });
      }
    } catch (err) {
      loginUserSel.innerHTML = '<option value="" disabled selected>Could not load users</option>';
    }
  }

  loadUsers();

  // --- Login ---
  loginForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    loginError.textContent = '';
    const name = loginUserSel.value;
    const pin  = loginPinIn.value;

    if (!name) {
      loginError.textContent = 'Please select a user.';
      return;
    }
    if (!pin) {
      loginError.textContent = 'Please enter your PIN.';
      return;
    }

    loginBtn.disabled = true;
    loginBtn.textContent = 'Signing in\u2026';

    try {
      const res = await fetch('/users/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, pin }),
      });

      if (res.ok) {
        window.location.href = '/';
      } else if (res.status === 401) {
        loginError.textContent = 'Incorrect PIN. Please try again.';
      } else if (res.status === 429) {
        loginError.textContent = 'Too many attempts. Try again in 15 minutes.';
      } else {
        loginError.textContent = 'Login failed. Please try again.';
      }
    } catch (err) {
      loginError.textContent = 'Network error. Is the server running?';
    } finally {
      loginBtn.disabled = false;
      loginBtn.textContent = 'Sign In';
    }
  });

  // --- Register ---
  registerForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    registerError.textContent = '';
    const name = regNameIn.value.trim();
    const pin  = regPinIn.value;

    if (!name) {
      registerError.textContent = 'Please enter a name.';
      return;
    }
    if (pin.length < 4) {
      registerError.textContent = 'PIN must be at least 4 characters.';
      return;
    }

    registerBtn.disabled = true;
    registerBtn.textContent = 'Creating account\u2026';

    try {
      const res = await fetch('/users/register', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, pin }),
      });

      if (res.ok) {
        window.location.href = '/';
      } else if (res.status === 409) {
        registerError.textContent = 'That name is already taken. Choose another.';
      } else {
        registerError.textContent = 'Registration failed. Please try again.';
      }
    } catch (err) {
      registerError.textContent = 'Network error. Is the server running?';
    } finally {
      registerBtn.disabled = false;
      registerBtn.textContent = 'Create Account';
    }
  });
})();
