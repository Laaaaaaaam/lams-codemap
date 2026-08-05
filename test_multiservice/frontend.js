// frontend.js — 前端 Web 应用 (JavaScript)

const API_BASE = 'http://localhost:3000/api';

// API 客户端
class ApiClient {
    constructor(baseUrl) {
        this.baseUrl = baseUrl || API_BASE;
        this.token = null;
    }

    async login(username, password) {
        const response = await fetch(`${this.baseUrl}/login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password })
        });
        
        if (!response.ok) {
            throw new Error('Login failed');
        }
        
        const data = await response.json();
        this.token = data.token;
        return data;
    }

    async request(path, options = {}) {
        if (!this.token) {
            throw new Error('Not authenticated');
        }

        const response = await fetch(`${this.baseUrl}${path}`, {
            ...options,
            headers: {
                'Content-Type': 'application/json',
                'Authorization': this.token,
                ...options.headers
            }
        });

        if (response.status === 401) {
            this.token = null;
            throw new Error('Token expired');
        }

        if (response.status === 429) {
            throw new Error('Rate limited');
        }

        return response;
    }

    async getUsers() {
        const resp = await this.request('/users');
        return resp.json();
    }

    async createUser(username, email) {
        const resp = await this.request('/users', {
            method: 'POST',
            body: JSON.stringify({ username, email })
        });
        return resp.json();
    }

    async deleteUser(id) {
        await this.request('/users', {
            method: 'DELETE',
            body: JSON.stringify({ id })
        });
    }
}

// UI 控制器
class UIController {
    constructor(apiClient) {
        this.api = apiClient;
        this.users = [];
    }

    async init() {
        const loginBtn = document.getElementById('login-btn');
        const userList = document.getElementById('user-list');

        if (loginBtn) {
            loginBtn.addEventListener('click', () => this.handleLogin());
        }
    }

    async handleLogin() {
        const username = document.getElementById('username').value;
        const password = document.getElementById('password').value;

        try {
            await this.api.login(username, password);
            await this.loadUsers();
        } catch (error) {
            this.showError(error.message);
        }
    }

    async loadUsers() {
        try {
            this.users = await this.api.getUsers();
            this.renderUsers();
        } catch (error) {
            this.showError(error.message);
        }
    }

    renderUsers() {
        const list = document.getElementById('user-list');
        if (!list) return;

        list.innerHTML = this.users.map(user => `
            <div class="user-card">
                <span>${user.username}</span>
                <span>${user.email}</span>
                <button onclick="ui.deleteUser(${user.id})">Delete</button>
            </div>
        `).join('');
    }

    async deleteUser(id) {
        try {
            await this.api.deleteUser(id);
            this.users = this.users.filter(u => u.id !== id);
            this.renderUsers();
        } catch (error) {
            this.showError(error.message);
        }
    }

    showError(message) {
        const errorDiv = document.getElementById('error');
        if (errorDiv) {
            errorDiv.textContent = message;
            errorDiv.style.display = 'block';
        }
    }
}

// 初始化
const client = new ApiClient();
const ui = new UIController(client);

document.addEventListener('DOMContentLoaded', () => {
    ui.init();
});

// 导出
module.exports = { ApiClient, UIController };
