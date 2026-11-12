/* QuickPDF QR — shared form behaviour: validation, selection feedback, submit state */
(function () {
    const LIMIT_MB = parseInt(document.body.dataset.limitMb || '4', 10);
    const LIMIT_BYTES = LIMIT_MB * 1024 * 1024;

    function humanSize(bytes) {
        const mb = bytes / 1048576;
        return mb >= 0.1
            ? mb.toFixed(2) + ' MB'
            : Math.max(1, Math.round(bytes / 1024)) + ' KB';
    }

    function matchesAccept(file, accept) {
        if (!accept) return true;
        const name = (file.name || '').toLowerCase();
        const type = (file.type || '').toLowerCase();
        return accept.split(',').some(function (rawRule) {
            const rule = rawRule.trim().toLowerCase();
            if (!rule) return false;
            if (rule.charAt(0) === '.') return name.endsWith(rule);
            if (rule.slice(-2) === '/*') return type.indexOf(rule.slice(0, -1)) === 0;
            return type === rule;
        });
    }

    document.querySelectorAll('form.tool-form').forEach(function (form) {
        const input = form.querySelector('input[type="file"]');
        if (!input) return;

        const picked = form.querySelector('.picked');
        const clear = form.querySelector('.clear');
        const warn = form.querySelector('.warn');
        const submit = form.querySelector('.submit');
        const drop = form.querySelector('.drop');
        const modes = Array.prototype.slice.call(form.querySelectorAll('input[name="mode"]'));
        const busyLabel = form.dataset.busy || 'Working…';
        let busy = false;

        function check(files) {
            if (!files.length) return { ok: true, message: '' };
            const bad = files.find(function (file) {
                return !matchesAccept(file, input.accept);
            });
            if (bad) {
                return {
                    ok: false,
                    message: '“' + bad.name + '” can’t be used here — check the formats listed below.'
                };
            }
            const total = files.reduce(function (sum, file) { return sum + file.size; }, 0);
            if (total > LIMIT_BYTES) {
                return {
                    ok: false,
                    message: 'That is ' + humanSize(total) + ' — the limit is ' + LIMIT_MB + ' MB per upload.'
                };
            }
            return { ok: true, message: '' };
        }

        function render() {
            if (busy) return;
            const files = Array.prototype.slice.call(input.files || []);
            const result = check(files);

            if (picked) {
                if (!files.length) {
                    picked.textContent = '';
                } else {
                    const names = files.slice(0, 3).map(function (file) { return file.name; }).join(', ');
                    const more = files.length > 3 ? ' +' + (files.length - 3) + ' more' : '';
                    const total = files.reduce(function (sum, file) { return sum + file.size; }, 0);
                    picked.textContent = names + more + ' · ' + humanSize(total);
                }
            }
            if (clear) clear.hidden = files.length === 0;
            if (warn) {
                warn.textContent = result.message;
                warn.hidden = result.ok;
            }
            if (submit) submit.disabled = files.length > 0 && !result.ok;
        }

        input.addEventListener('change', render);

        if (clear) {
            clear.addEventListener('click', function () {
                input.value = '';
                render();
            });
        }

        modes.forEach(function (radio) {
            radio.addEventListener('change', function () {
                const checked = modes.filter(function (item) { return item.checked; })[0];
                const current = checked ? checked.value : null;
                const allowMultiple = form.dataset.multiWhen
                    ? current === form.dataset.multiWhen
                    : false;
                if (input.multiple !== allowMultiple) {
                    input.multiple = allowMultiple;
                    input.value = '';
                    render();
                }
            });
        });

        if (drop) {
            ['dragenter', 'dragover'].forEach(function (type) {
                drop.addEventListener(type, function (event) {
                    event.preventDefault();
                    drop.classList.add('is-active');
                });
            });
            ['dragleave', 'drop'].forEach(function (type) {
                drop.addEventListener(type, function (event) {
                    event.preventDefault();
                    drop.classList.remove('is-active');
                });
            });
            drop.addEventListener('drop', function (event) {
                if (event.dataTransfer && event.dataTransfer.files.length) {
                    input.files = event.dataTransfer.files;
                    render();
                }
            });
        }

        form.addEventListener('submit', function (event) {
            const result = check(Array.prototype.slice.call(input.files || []));
            if (!result.ok) {
                event.preventDefault();
                render();
                return;
            }
            busy = true;
            if (submit) {
                submit.disabled = true;
                submit.textContent = busyLabel;
            }
        });

        render();
    });
})();
