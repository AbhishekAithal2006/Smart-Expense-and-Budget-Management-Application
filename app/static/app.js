const $ = (selector) => document.querySelector(selector);
const state = { csrf: '', mode: 'login', page: 'home', toastTimer: null, currencyCode: 'INR', isAdmin: false, userEmail: '' };
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

function showApp(email, fullName = '', isAdmin = false) {
  $('#auth-view').classList.add('hidden');
  $('#app-view').classList.remove('hidden');
  showPage('home');
  $('#user-email').textContent = email;
  state.userEmail = String(email || '').toLowerCase();
  $('#user-email').classList.remove('hidden');
  $('#profile-button').classList.remove('hidden');
  state.isAdmin = Boolean(isAdmin);
  $('#admin-button').classList.toggle('hidden', !state.isAdmin);
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
  $('#admin-button').classList.add('hidden');
  $('#profile-view').classList.add('hidden');
  $('#home-view').classList.remove('hidden');
  state.page = 'home';
  state.isAdmin = false;
  state.userEmail = '';
  $('#profile-button').textContent = 'Profile';
  state.csrf = '';
}

function showPage(page) {
  state.page = page;
  const isProfile = page === 'profile';
  const isAdminPage = page === 'admin';
  $('#home-view').classList.toggle('hidden', isProfile || isAdminPage);
  $('#profile-view').classList.toggle('hidden', !isProfile);
  $('#admin-view').classList.toggle('hidden', !isAdminPage);
  $('#profile-button').textContent = isProfile || isAdminPage ? 'Home' : 'Profile';
  $('#profile-button').setAttribute('aria-label', isProfile || isAdminPage ? 'Return to home dashboard' : 'Open profile');
  $('#admin-button').textContent = isAdminPage ? 'Close admin' : 'Admin';
}

async function loadAdminData() {
  const [usersData, activityData] = await Promise.all([api('/api/admin/users'), api('/api/admin/activity')]);
  const userRows = $('#admin-user-rows');
  userRows.replaceChildren();
  $('#admin-users-empty').classList.toggle('hidden', usersData.users.length !== 0);
  for (const user of usersData.users) {
    const row = document.createElement('tr');
    row.append(element('td', '', user.full_name));
    row.append(element('td', '', user.email));
    row.append(element('td', '', new Date(user.created_at).toLocaleDateString()));
    row.append(element('td', '', user.is_admin ? 'Administrator' : 'User'));
    row.append(element('td', '', user.is_suspended ? 'Suspended' : 'Active'));
    const actions = document.createElement('td');
    if (!user.is_admin || user.email.toLowerCase() !== state.userEmail) {
      const button = element('button', 'row-action', user.is_suspended ? 'Reactivate' : 'Suspend');
      button.type = 'button';
      button.addEventListener('click', async () => {
        button.disabled = true;
        try {
          await api(`/api/admin/users/${user.id}`, { method: 'PATCH', body: JSON.stringify({ suspended: !user.is_suspended }) });
          await loadAdminData();
          toast(user.is_suspended ? 'Account reactivated.' : 'Account suspended and signed out.');
        } catch (error) { button.disabled = false; toast(error.message); }
      });
      actions.append(button);
    }
    row.append(actions);
    userRows.append(row);
  }
  const activityRows = $('#admin-activity-rows');
  activityRows.replaceChildren();
  $('#admin-activity-empty').classList.toggle('hidden', activityData.events.length !== 0);
  for (const event of activityData.events) {
    const row = document.createElement('tr');
    row.append(element('td', '', new Date(event.created_at).toLocaleString(undefined, { timeZone: 'UTC' })));
    row.append(element('td', '', event.event_type.replaceAll('_', ' ')));
    row.append(element('td', '', event.actor_user_id ?? '—'));
    row.append(element('td', '', event.target_user_id ?? '—'));
    activityRows.append(row);
  }
}

async function refresh() {
  try {
    await loadProfile();
    await Promise.all([loadCategories(), loadSummary(), loadExpenses(),loadBudgets()]);
  } catch (error) {
    if (error.message.includes('log in')) showAuth();
    else toast(error.message);
  }
}

async function loadCategories() {
  const data = await api('/api/categories');
  const expenseSelect = $('#expense-category');
  const filterSelect = $('#category-filter');
  const selectedExpense = expenseSelect.value;
  const selectedFilter = filterSelect.value;
  expenseSelect.replaceChildren(new Option('Choose category', ''));
  filterSelect.replaceChildren(new Option('All categories', ''));
  for (const category of [...data.predefined, ...data.custom.map((item) => item.name)]) {
    expenseSelect.add(new Option(category, category));
    filterSelect.add(new Option(category, category));
  }
  if ([...expenseSelect.options].some((option) => option.value === selectedExpense)) expenseSelect.value = selectedExpense;
  if ([...filterSelect.options].some((option) => option.value === selectedFilter)) filterSelect.value = selectedFilter;

  const list = $('#custom-category-list');
  list.replaceChildren();
  for (const category of data.custom) {
    const item = element('li', 'category-item');
    item.append(element('span', 'category-item-name', category.name));
    const edit = element('button', 'row-action', 'Edit');
    edit.type = 'button';
    edit.setAttribute('aria-label', `Rename ${category.name} category`);
    edit.addEventListener('click', () => startCategoryEdit(item, category));
    item.append(edit);
    list.append(item);
  }
}

function startCategoryEdit(item, category) {
  item.replaceChildren();
  const input = document.createElement('input');
  input.type = 'text';
  input.maxLength = 40;
  input.value = category.name;
  input.setAttribute('aria-label', `New name for ${category.name}`);
  const save = element('button', 'row-action', 'Save');
  save.type = 'button';
  const cancel = element('button', 'row-action', 'Cancel');
  cancel.type = 'button';
  save.addEventListener('click', async () => {
    $('#category-feedback').textContent = '';
    try {
      await api(`/api/categories/${category.id}`, { method: 'PATCH', body: JSON.stringify({ name: input.value }) });
      $('#category-feedback').textContent = 'Category updated. Matching expenses were updated too.';
      await loadCategories();
      await loadExpenses();
    } catch (error) { $('#category-feedback').textContent = error.message; }
  });
  cancel.addEventListener('click', () => loadCategories().catch((error) => toast(error.message)));
  item.append(input, save, cancel);
  input.focus();
  input.select();
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

  const emptyState = $('#empty-state');
  const hasFilters = params.toString().length > 0;

  emptyState.querySelector('h3').textContent = hasFilters
    ? 'No matching expenses found'
    : 'No expenses here yet';

  emptyState.querySelector('p').textContent = hasFilters
    ? 'Try changing your search or filters.'
    : 'Add your first expense above. Your history will build from there.';

  emptyState.classList.toggle('hidden', count > 0);
  $('.table-wrap').classList.toggle('hidden', count === 0);
}

function resetBudgetForm() {
  $('#budget-form').reset();
  $('#budget-id').value = '';
  $('#budget-type').value = 'monthly';
  $('#budget-error').textContent = '';
  $('#budget-submit').textContent = 'Save budget →';
  $('#budget-cancel').classList.add('hidden');
  setBudgetDates('monthly');
}

function setBudgetDates(type) {
  const today = new Date();
  let start;
  let end;

  if (type === 'weekly') {
    const day = today.getDay();
    const mondayOffset = (day + 6) % 7;
    start = new Date(today.getFullYear(), today.getMonth(), today.getDate() - mondayOffset);
    end = new Date(start.getFullYear(), start.getMonth(), start.getDate() + 6);
  } else if (type === 'yearly') {
    start = new Date(today.getFullYear(), 0, 1);
    end = new Date(today.getFullYear(), 11, 31);
  } else {
    start = new Date(today.getFullYear(), today.getMonth(), 1);
    end = new Date(today.getFullYear(), today.getMonth() + 1, 0);
  }

  $('#budget-start-date').value = localDate(start);
  $('#budget-end-date').value = localDate(end);
}

async function loadBudgets() {
  const result = await api('/api/budgets');
  const list = $('#budget-list');
  list.replaceChildren();

  const budgets = result.budgets || [];
  $('#budget-count').textContent =
    `${budgets.length} ${budgets.length === 1 ? 'budget' : 'budgets'}`;

  if (budgets.length === 0) {
    list.append(element(
      'p',
      'section-copy',
      'No budgets yet. Create your first budget above.'
    ));
    return;
  }

  const periodNames = {
    weekly: 'Weekly',
    monthly: 'Monthly',
    yearly: 'Yearly'
  };

  for (const budget of budgets) {
    const card = element('article', 'budget-item');
    const details = element('div', 'budget-item-details');

    details.append(
      element(
        'span',
        'budget-period',
        periodNames[budget.budget_type] || budget.budget_type
      ),
      element('strong', 'budget-amount', money(budget.amount_cents)),
      element(
        'span',
        'budget-dates',
        `${budget.start_date} – ${budget.end_date}`
      )
    );

    const actions = element('div', 'row-actions');

    const edit = element('button', 'row-action', 'Edit');
    edit.type = 'button';
    edit.addEventListener('click', () => startBudgetEdit(budget));

    const remove = element('button', 'row-action', 'Delete');
    remove.type = 'button';
    remove.addEventListener('click', () => deleteBudget(budget.id));

    actions.append(edit, remove);
    card.append(details, actions);
    list.append(card);
  }
}

function startBudgetEdit(budget) {
  $('#budget-id').value = budget.id;
  $('#budget-type').value = budget.budget_type;
  $('#budget-amount').value = (budget.amount_cents / 100).toFixed(2);
  $('#budget-start-date').value = budget.start_date;
  $('#budget-end-date').value = budget.end_date;
  $('#budget-error').textContent = '';
  $('#budget-submit').textContent = 'Update budget →';
  $('#budget-cancel').classList.remove('hidden');

  $('#budgets-section').scrollIntoView({
    behavior: 'smooth',
    block: 'center'
  });

  $('#budget-amount').focus({ preventScroll: true });
}

async function deleteBudget(id) {
  if (!window.confirm('Delete this budget? This cannot be undone.')) {
    return;
  }

  try {
    await api(`/api/budgets?id=${encodeURIComponent(id)}`, {
      method: 'DELETE'
    });

    toast('Budget deleted.');
    await loadBudgets();
  } catch (error) {
    toast(error.message);
  }
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
    showApp(result.email, result.fullName, result.isAdmin);
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

$('#category-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const input = $('#category-name');
  const feedback = $('#category-feedback');
  feedback.textContent = '';
  try {
    await api('/api/categories', { method: 'POST', body: JSON.stringify({ name: input.value }) });
    input.value = '';
    feedback.textContent = 'Category added. It is now available when recording expenses.';
    await loadCategories();
  } catch (error) { feedback.textContent = error.message; }
});

$('#profile-button').addEventListener('click', async () => {
  const nextPage = state.page === 'profile' || state.page === 'admin' ? 'home' : 'profile';
  showPage(nextPage);
  if (nextPage === 'profile') {
    try { await loadProfile(); }
    catch (error) { toast(error.message); }
  }
  window.scrollTo({ top: 0, behavior: 'smooth' });
});
$('#admin-button').addEventListener('click', async () => {
  const nextPage = state.page === 'admin' ? 'home' : 'admin';
  showPage(nextPage);
  if (nextPage === 'admin') {
    try { await loadAdminData(); }
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

$('#budget-type').addEventListener('change', (event) => {
  setBudgetDates(event.currentTarget.value);
  $('#budget-error').textContent = '';
});

$('#budget-form').addEventListener('submit', async (event) => {
  event.preventDefault();

  const errorNode = $('#budget-error');
  errorNode.textContent = '';

  const budgetId = $('#budget-id').value;
  const budgetType = $('#budget-type').value;
  const amount = $('#budget-amount').value.trim();
  const startDate = $('#budget-start-date').value;
  const endDate = $('#budget-end-date').value;

  if (!amount || Number(amount) <= 0) {
    errorNode.textContent = 'Enter a budget amount greater than zero.';
    $('#budget-amount').focus();
    return;
  }

  if (!startDate || !endDate || startDate > endDate) {
    errorNode.textContent = 'Choose a valid budget date range.';
    return;
  }

  const data = {
    budgetType,
    amount,
    startDate,
    endDate
  };

  try {
    if (budgetId) {
      await api(`/api/budgets?id=${encodeURIComponent(budgetId)}`, {
        method: 'PATCH',
        body: JSON.stringify(data)
      });

      toast('Budget updated successfully.');
    } else {
      await api('/api/budgets', {
        method: 'POST',
        body: JSON.stringify(data)
      });

      toast('Budget created successfully.');
    }

    resetBudgetForm();
    await loadBudgets();
  } catch (error) {
    errorNode.textContent = error.message;
  }
});

$('#budget-cancel').addEventListener('click', resetBudgetForm);

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
const googleError = new URLSearchParams(window.location.search).get('auth_error');
const pageParams = new URLSearchParams(window.location.search);
const emailVerified = pageParams.get('email_verified') === '1';
const emailError = pageParams.get('email_error');
if (googleError) {
  $('#auth-error').textContent = googleErrors[googleError] || googleErrors.google_failed;
}
const emailErrors = {
  invalid: 'This verification link is invalid or has already been used.',
  expired: 'This verification link has expired. Request another email change from your profile.',
  already_used: 'That email address is already connected to another account.'
};
if (googleError || emailVerified || emailError) window.history.replaceState(null, '', '/');

api('/api/me').then((session) => {
  state.csrf = session.csrfToken || '';
  if (session.authenticated) showApp(session.email, '', session.isAdmin);
  if (emailVerified) {
    if (session.authenticated) toast('Your email address has been verified.');
    else $('#auth-error').textContent = 'Your email address is verified. Sign in with the new address.';
  } else if (emailError) {
    const message = emailErrors[emailError] || emailErrors.invalid;
    if (session.authenticated) toast(message);
    else $('#auth-error').textContent = message;
  }
}).catch(() => {});
