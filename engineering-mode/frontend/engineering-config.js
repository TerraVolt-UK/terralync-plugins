/**
 * Engineering Mode — plugin-provided UI controller
 * ==================================================
 *
 * This file is served from the engineering-mode plugin's frontend/
 * directory.  The main dashboard dynamically loads it when the plugin
 * is detected.  It provides the `window.EngineeringMode` class that
 * config-manager.js relies on for unlock/lock/command execution.
 *
 * ⚠️  DO NOT SHIP THIS FILE IN PRODUCTION BUILDS
 */

class EngineeringMode {
    constructor(dashboard) {
        this.dashboard = dashboard;
        this.enabled = false;
        this._commands = [];

        // Listen for engineering responses from the API client
        if (dashboard.eventBus) {
            dashboard.eventBus.on('engineering_response', (data) => this._handleResponse(data));
        }

        // Check initial state
        this._checkState();
    }

    // ─── State management ──────────────────────────────────────────

    async _checkState() {
        try {
            const client = this.dashboard?.modules?.api;
            if (!client) return;

            client.send({ type: 'engineering_command', command: 'get_engineering_info' });
        } catch (e) {
            console.warn('Engineering: failed to check state', e);
        }
    }

    _handleResponse(data) {
        console.log('Engineering response received:', data);
        // Update internal state from server responses
        if (data.available !== undefined) {
            this._commands = data.commands || [];
        }
        if (data.enabled !== undefined) {
            const wasEnabled = this.enabled;
            this.enabled = !!data.enabled;
            console.log('Engineering enabled changed:', wasEnabled, '->', this.enabled);
            if (this.enabled !== wasEnabled) {
                this._updateUI();
            }
        }
        if (data.success !== undefined && data.message) {
            const type = data.success ? 'success' : 'error';
            this.showNotification(data.message, type);
        }
    }

    // ─── Public interface (used by config-manager.js) ──────────────

    showUnlockDialog() {
        const code = prompt(
            '⚠️ ENGINEERING MODE ⚠️\n\n' +
            'Enter unlock code to enable engineering commands.\n' +
            'Incorrect use can void warranty and damage equipment.\n\n' +
            'Unlock code:'
        );
        if (code === null) return; // cancelled

        const client = this.dashboard?.modules?.api;
        if (client) {
            client.send({
                type: 'engineering_command',
                command: 'engineering_unlock',
                value: { code },
            });
        }
    }

    lock() {
        const client = this.dashboard?.modules?.api;
        if (client) {
            client.send({ type: 'engineering_command', command: 'engineering_lock' });
        }
        this.enabled = false;
        this._updateUI();
    }

    async executeCommand(command, value, description, dangerLevel) {
        if (!this.enabled) {
            this.showNotification('Engineering mode not unlocked', 'warning');
            return;
        }

        // Confirmation for dangerous operations
        if (dangerLevel === 'CRITICAL') {
            if (!confirm(
                `⚠️ CRITICAL OPERATION ⚠️\n\n${description}\n\n` +
                'This cannot be undone. Are you sure?'
            )) return;
        } else if (dangerLevel === 'HIGH') {
            if (!confirm(`⚠️ ${description}\n\nContinue?`)) return;
        }

        const client = this.dashboard?.modules?.api;
        if (client) {
            // For commission_preset, the value is { preset: "..." }
            const sendValue = (command === 'commission_preset' && typeof value === 'object')
                ? value.preset
                : value;
            client.send({
                type: 'engineering_command',
                command,
                value: sendValue,
            });
        }
    }

    showNotification(message, type = 'info') {
        const colors = {
            success: 'bg-green-700',
            error: 'bg-red-700',
            warning: 'bg-yellow-700',
            info: 'bg-blue-700',
        };
        const bg = colors[type] || colors.info;

        const el = document.createElement('div');
        el.className = `fixed bottom-4 right-4 ${bg} text-white px-6 py-3 rounded-lg shadow-lg z-50`;
        el.innerHTML = `
            <div class="flex items-center">
                <i class="fas fa-info-circle mr-2"></i>
                <span>${message}</span>
            </div>
        `;
        document.body.appendChild(el);
        setTimeout(() => el.remove(), 4000);
    }

    // ─── UI state ──────────────────────────────────────────────────

    _updateUI() {
        // Update status badge
        const statusEl = document.getElementById('engineering-status');
        if (statusEl) {
            if (this.enabled) {
                statusEl.textContent = 'UNLOCKED';
                statusEl.className = 'ml-auto px-3 py-1 text-xs font-bold rounded bg-red-600 text-white';
            } else {
                statusEl.textContent = 'LOCKED';
                statusEl.className = 'ml-auto px-3 py-1 text-xs font-bold rounded bg-gray-600 text-white';
            }
        }

        // Toggle unlock/lock buttons
        const unlockBtn = document.getElementById('engineering-unlock-btn');
        const lockBtn = document.getElementById('engineering-lock-btn');
        if (unlockBtn) unlockBtn.classList.toggle('hidden', this.enabled);
        if (lockBtn) lockBtn.classList.toggle('hidden', !this.enabled);

        // Enable/disable all engineering controls
        document.querySelectorAll('.engineering-control').forEach(el => {
            if (this.enabled) {
                el.disabled = false;
                el.classList.remove('opacity-50', 'cursor-not-allowed');
            } else {
                el.disabled = true;
                el.classList.add('opacity-50', 'cursor-not-allowed');
            }
        });
    }
}

// Expose globally so main.js and config-manager.js can find it
window.EngineeringMode = EngineeringMode;
