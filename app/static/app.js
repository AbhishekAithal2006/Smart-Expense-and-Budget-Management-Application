const $ = (selector) => document.querySelector(selector);
const state = { csrf: '', mode: 'login', page: 'home', toastTimer: null, currencyCode: 'INR' };
const currencySymbols = { INR: '₹', USD: '$', EUR: '€', GBP: '£' };
const normalizeCurrencyCode = (value) => Object.hasOwn(currencySymbols, value) ? value : 'INR';
const money = (cents) => new Intl.NumberFormat('en-IN', { style: 'currency', currency: normalizeCurrencyCode(state.currencyCode), maximumFractionDigits: 2 }).format((cents || 0) / 100);
const localDate = (date = new Date()) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
const monthNow = () => localDate().slice(0, 7);

async function api(url, options = {}) {
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  if (options.method && options.method !== 'GET') headers['X-CSRF-Token'] = state.csrf;
  const response = await fetch(url, { credentials: 'same-origin', ...options, headers });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || 'Something went wrong. Please try again.');
  if (payload.csrfToken) state.csrf = payload.csrfToken;
  return payload;
}

function toast(message) {
  const node = $('#toast');
  node.textContent = message;
  node.classList.add('show');
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => node.classList.remove('show'), 2800);
}

function setAuthMode(mode) {
  state.mode = mode;
  const login = mode === 'login';
  $('#login-tab').classList.toggle('active', login);
  $('#register-tab').classList.toggle('active', !login);
  $('#login-tab').setAttribute('aria-selected', String(login));
  $('#register-tab').setAttribute('aria-selected', String(!login));
  $('#auth-title').textContent = login ? 'Welcome back' : 'Make yourself at home';
  $('#auth-subtitle').textContent = login ? 'Pick up right where you left off.' : 'Create an account to start tracking.';
  $('#auth-submit').innerHTML = `${login ? 'Log in' : 'Create account'} <span aria-hidden="true">→</span>`;
  $('#auth-password').autocomplete = login ? 'current-password' : 'new-password';
  $('#register-name-field').classList.toggle('hidden', login);
  $('#auth-name').required = !login;
  $('#auth-error').textContent = '';
}

function showApp(email, fullName = '') {
  $('#auth-view').classList.add('hidden');
  $('#app-view').classList.remove('hidden');
  showPage('home');
  $('#user-email').textContent = email;
  $('#user-email').classList.remove('hidden');
  $('#profile-button').classList.remove('hidden');
  $('#logout-button').classList.remove('hidden');
  $('#profile-name-summary').textContent = fullName || '—';
  $('#auth-password').value = '';
  $('#expense-date').value = localDate();
  $('#month-filter').value = monthNow();
  $('#month-label').textContent = new Date().toLocaleDateString('en-IN', { month: 'long', year: 'numeric' }).toUpperCase();
  refresh();
}

function showAuth() {
  $('#app-view').classList.add('hidden');
  $('#auth-view').classList.remove('hidden');
  $('#user-email').classList.add('hidden');
  $('#profile-button').classList.add('hidden');
  $('#logout-button').classList.add('hidden');
  $('#profile-view').classList.add('hidden');
  $('#home-view').classList.remove('hidden');
  state.page = 'home';
  $('#profile-button').textContent = 'Profile';
  state.csrf = '';
}

function showPage(page) {
  state.page = page;
  const isProfile = page === 'profile';
  $('#home-view').classList.toggle('hidden', isProfile);
  $('#profile-view').classList.toggle('hidden', !isProfile);
  $('#profile-button').textContent = isProfile ? 'Home' : 'Profile';
  $('#profile-button').setAttribute('aria-label', isProfile ? 'Return to home dashboard' : 'Open profile');
}

async function refresh() {
  try {
    await loadProfile();
    await Promise.all([loadSummary(), loadExpenses()]);
  } catch (error) {
    if (error.message.includes('log in')) showAuth();
    else toast(error.message);
  }
}

async function loadSummary() {
  const summary = await api('/api/summary');
  $('#spent-total').textContent = money(summary.spentCents);
  $('#month-transaction-count').textContent = summary.expenseCount;
  $('#spent-status').textContent = `${summary.expenseCount} ${summary.expenseCount === 1 ? 'transaction' : 'transactions'} this month`;
}

async function loadProfile() {
  const profile = await api('/api/profile');
  $('#profile-name').value = profile.fullName;
  $('#profile-email').value = profile.email;
  $('#profile-email-pending').classList.toggle('hidden', !profile.pendingEmail);
  $('#profile-email-pending').textContent = profile.pendingEmail
    ? `Email change waiting for verification. Check ${profile.pendingEmail}; your current email stays active until you verify it.`
    : '';
  $('#profile-mobile').value = profile.mobile;
  state.currencyCode = normalizeCurrencyCode(profile.currencyCode);
  $('#profile-currency').value = state.currencyCode;
  $('#amount-currency-symbol').textContent = currencySymbols[state.currencyCode];
  $('#profile-name-summary').textContent = profile.fullName || '—';
  $('#user-email').textContent = profile.email;
  $('#profile-display-name').textContent = profile.fullName || 'Your account';
  $('#profile-display-email').textContent = profile.email;
  const loginMethod = profile.loginMethod || 'Sign-in details unavailable';
  $('#profile-login-badge').textContent = profile.loginMethod ? `${loginMethod} sign-in` : loginMethod;
  $('#profile-sign-in-method').textContent = loginMethod;
  const createdAt = profile.createdAt ? new Date(profile.createdAt) : null;
  $('#profile-created-at').textContent = !createdAt || Number.isNaN(createdAt.getTime()) ? '—' : createdAt.toLocaleDateString('en-IN', { day: 'numeric', month: 'long', year: 'numeric' });
  $('#password-section').classList.toggle('hidden', profile.loginMethod === 'Google');
  setProfileAvatar(profile.avatarUrl, profile.fullName);
}

function setProfileAvatar(url, name) {
  const image = $('#profile-avatar');
  const fallback = $('#profile-avatar-initials');
  const initials = (name || 'User').trim().split(/\s+/).slice(0, 2).map((part) => part[0]).join('').toUpperCase();
  fallback.textContent = initials || 'U';
  fallback.classList.remove('hidden');
  image.classList.add('hidden');
  image.onerror = () => {
    image.classList.add('hidden');
    fallback.classList.remove('hidden');
  };
  if (typeof url === 'string' && /^https:\/\/lh\d+\.googleusercontent\.com\//.test(url)) {
    image.src = url;
    image.classList.remove('hidden');
    fallback.classList.add('hidden');
  } else {
    image.removeAttribute('src');
  }
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function categorySymbol(category) {
  return ({ Food: '✳', Travel: '↗', Shopping: '◇', Medical: '+', Entertainment: '♫', Home: '⌂', Utilities: '⌁' })[category] || '•';
}

function addActionCell(row, expense) {
  const cell = element('td');
  if (expense.deleted_at) {
    cell.append(element('span', 'deleted-expense-badge', 'Deleted'));
    return row.append(cell);
  }
  const actions = element('div', 'row-actions');
  const edit = element('button', 'row-action', 'Edit');
  edit.type = 'button';
  edit.setAttribute('aria-label', `Edit ${expense.category} expense`);
  edit.addEventListener('click', () => editExpense(expense));
  const remove = element('button', 'row-action', 'Delete');
  remove.type = 'button';
  remove.setAttribute('aria-label', `Delete ${expense.category} expense`);
  remove.addEventListener('click', () => deleteExpense(expense.id));
  actions.append(edit, remove);
  cell.append(actions);
  row.append(cell);
}

async function loadExpenses() {
  const params = new URLSearchParams();
  if ($('#search-input').value.trim()) params.set('q', $('#search-input').value.trim());
  if ($('#category-filter').value) params.set('category', $('#category-filter').value);
  if ($('#month-filter').value) params.set('month', $('#month-filter').value);
  const result = await api(`/api/expenses?${params.toString()}`);
  const rows = $('#expense-rows');
  rows.replaceChildren();
  for (const expense of result.expenses) {
    const row = document.createElement('tr');
    const detail = element('td');
    const expenseName = element('span', 'expense-name');
    expenseName.append(element('span', 'category-mark', categorySymbol(expense.category)));
    const text = document.createElement('span');
    text.append(document.createTextNode(expense.category));
    if (expense.description) text.append(element('span', 'expense-description', expense.description));
    if (expense.deleted_at) {
      const deletedAt = new Date(expense.deleted_at);
      const deletedLabel = Number.isNaN(deletedAt.getTime()) ? 'Deleted' : `Deleted on ${deletedAt.toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' })}`;
      text.append(element('span', 'expense-deleted-note', deletedLabel));
      row.classList.add('expense-deleted-row');
    }
    expenseName.append(text);
    detail.append(expenseName);
    const dateCell = element('td', '', new Date(`${expense.spent_on}T00:00:00`).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' }));
    const payment = element('td', '', expense.payment_method || '—');
    const amount = element('td', 'amount-cell', money(expense.amount_cents));
    row.append(detail, dateCell, payment, amount);
    addActionCell(row, expense);
    rows.append(row);
  }
  const count = result.expenses.length;
  $('#expense-count').textContent = `${count} ${count === 1 ? 'item' : 'items'}`;
  $('#empty-state').classList.toggle('hidden', count > 0);
  $('.table-wrap').classList.toggle('hidden', count === 0);
}

async function editExpense(expense) {
  const amount = window.prompt('Amount in rupees', (expense.amount_cents / 100).toFixed(2));
  if (amount === null) return;
  const category = window.prompt('Category', expense.category);
  if (category === null) return;
  const description = window.prompt('Description', expense.description);
  if (description === null) return;
  const date = window.prompt('Date (YYYY-MM-DD)', expense.spent_on);
  if (date === null) return;
  const paymentMethod = window.prompt('Payment method', expense.payment_method);
  if (paymentMethod === null) return;
  try {
    await api(`/api/expenses/${expense.id}`, { method: 'PATCH', body: JSON.stringify({ amount, category, description, date, paymentMethod }) });
    toast('Expense updated.');
    await refresh();
  } catch (error) { toast(error.message); }
}

async function deleteExpense(id) {
  if (!window.confirm('Delete this expense? This cannot be undone.')) return;
  try {
    await api(`/api/expenses/${id}`, { method: 'DELETE' });
    toast('Expense deleted.');
    await refresh();
  } catch (error) { toast(error.message); }
}

$('#today-label').textContent = new Date().toLocaleDateString('en-IN', { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' });
$('#login-tab').addEventListener('click', () => setAuthMode('login'));
$('#register-tab').addEventListener('click', () => setAuthMode('register'));
$('#auth-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  $('#auth-error').textContent = '';
  const data = Object.fromEntries(new FormData(event.currentTarget));
  try {
    const result = await api(state.mode === 'login' ? '/api/login' : '/api/register', { method: 'POST', body: JSON.stringify(data) });
    state.csrf = result.csrfToken;
    showApp(result.email, result.fullName);
  } catch (error) { $('#auth-error').textContent = error.message; }
});

$('#logout-button').addEventListener('click', async () => {
  try { await api('/api/logout', { method: 'POST', body: '{}' }); } catch (_) { /* the session may already have expired */ }
  showAuth();
});

$('#expense-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  $('#expense-error').textContent = '';
  const data = Object.fromEntries(new FormData(form));
  try {
    await api('/api/expenses', { method: 'POST', body: JSON.stringify(data) });
    form.reset();
    $('#expense-date').value = localDate();
    toast('Expense saved.');
    await refresh();
  } catch (error) { $('#expense-error').textContent = error.message; }
});

$('#profile-button').addEventListener('click', async () => {
  const nextPage = state.page === 'profile' ? 'home' : 'profile';
  showPage(nextPage);
  if (nextPage === 'profile') {
    try { await loadProfile(); }
    catch (error) { toast(error.message); }
  }
  window.scrollTo({ top: 0, behavior: 'smooth' });
});
$('#profile-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  $('#profile-error').textContent = '';
  $('#profile-success').textContent = '';
  const data = Object.fromEntries(new FormData(event.currentTarget));
  const email = data.email.trim();
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(email)) {
    $('#profile-error').textContent = 'Enter a valid email address.';
    $('#profile-email').focus();
    return;
  }
  const mobile = data.mobile.trim();
  const mobileDigits = mobile.replace(/\D/g, '').length;
  if (mobile && (!/^\+?[0-9][0-9 ()-]{5,28}$/.test(mobile) || mobileDigits < 10 || mobileDigits > 15)) {
    $('#profile-error').textContent = 'Enter a valid mobile number with 10–15 digits, or leave it empty.';
    $('#profile-mobile').focus();
    return;
  }
  try {
    const profile = await api('/api/profile', { method: 'PATCH', body: JSON.stringify(data) });
    state.currencyCode = normalizeCurrencyCode(profile.currencyCode || data.currencyCode);
    $('#profile-currency').value = state.currencyCode;
    $('#amount-currency-symbol').textContent = currencySymbols[state.currencyCode];
    $('#user-email').textContent = profile.email;
    $('#profile-email').value = profile.email;
    $('#profile-name-summary').textContent = profile.fullName;
    $('#profile-display-name').textContent = profile.fullName;
    $('#profile-display-email').textContent = profile.email;
    $('#profile-email-pending').classList.toggle('hidden', !profile.emailVerificationPending);
    $('#profile-email-pending').textContent = profile.emailVerificationPending
      ? `Verification link sent to ${profile.pendingEmail}. Your current email remains active until you verify it.`
      : '';
    $('#profile-success').textContent = profile.emailVerificationPending
      ? 'Profile details saved. Verify the new address to finish changing your email.'
      : 'Profile saved successfully.';
    toast(profile.emailVerificationPending ? 'Check your new email to verify the change.' : 'Profile updated.');
    await Promise.all([loadSummary(), loadExpenses()]);
  } catch (error) { $('#profile-error').textContent = error.message; }
});

['#profile-name', '#profile-email', '#profile-mobile', '#profile-currency'].forEach((selector) => {
  const field = $(selector);
  const clearProfileFeedback = () => {
    $('#profile-error').textContent = '';
    $('#profile-success').textContent = '';
  };
  field.addEventListener('input', clearProfileFeedback);
  field.addEventListener('change', clearProfileFeedback);
});

$('#profile-currency').addEventListener('change', (event) => {
  $('#amount-currency-symbol').textContent = currencySymbols[event.currentTarget.value] || currencySymbols.INR;
});

$('#password-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const errorNode = $('#password-error');
  errorNode.textContent = '';
  const data = Object.fromEntries(new FormData(form));
  if (data.newPassword !== data.confirmPassword) {
    errorNode.textContent = 'The new passwords do not match.';
    $('#confirm-password').focus();
    return;
  }
  try {
    await api('/api/password', { method: 'POST', body: JSON.stringify(data) });
    form.reset();
    toast('Password updated. Other signed-in sessions were logged out.');
  } catch (error) { errorNode.textContent = error.message; }
});

$('#filter-form').addEventListener('submit', async (event) => { event.preventDefault(); try { await loadExpenses(); } catch (error) { toast(error.message); } });
$('#clear-filters').addEventListener('click', async () => {
  $('#search-input').value = '';
  $('#category-filter').value = '';
  $('#month-filter').value = '';
  try { await loadExpenses(); } catch (error) { toast(error.message); }
});
$('#add-expense-shortcut').addEventListener('click', () => {
  $('#expense-form-card').scrollIntoView({ behavior: 'smooth', block: 'center' });
  $('#expense-amount').focus({ preventScroll: true });
});
document.addEventListener('keydown', (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k' && !$('#app-view').classList.contains('hidden')) {
    event.preventDefault();
    $('#search-input').focus();
  }
});

const googleErrors = {
  google_not_configured: 'Google sign-in is not configured yet. Add the Google OAuth environment variables to enable it.',
  google_cancelled: 'Google sign-in was cancelled.',
  google_state: 'Google sign-in could not be verified. Please try again.',
  google_account_conflict: 'This email is already linked to a different Google account.',
  google_failed: 'Google sign-in failed. Please try again.'
};
const pageParams = new URLSearchParams(window.location.search);
const googleError = pageParams.get('auth_error');
const emailVerified = pageParams.get('email_verified') === '1';
const emailError = pageParams.get('email_error');
if (googleError) $('#auth-error').textContent = googleErrors[googleError] || googleErrors.google_failed;
const emailErrors = {
  invalid: 'This verification link is invalid or has already been used.',
  expired: 'This verification link has expired. Request another email change from your profile.',
  already_used: 'That email address is already connected to another account.'
};
if (googleError || emailVerified || emailError) window.history.replaceState(null, '', '/');

api('/api/me').then((session) => {
  state.csrf = session.csrfToken || '';
  if (session.authenticated) showApp(session.email);
  if (emailVerified) {
    if (session.authenticated) toast('Your email address has been verified.');
    else $('#auth-error').textContent = 'Your email address is verified. Sign in with the new address.';
  } else if (emailError) {
    const message = emailErrors[emailError] || emailErrors.invalid;
    if (session.authenticated) toast(message);
    else $('#auth-error').textContent = message;
  }
}).catch(() => {});
