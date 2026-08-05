// types.ts — 前端 TypeScript 类型定义

interface User {
    id: number;
    username: string;
    email: string;
    role: string;
}

interface LoginRequest {
    username: string;
    password: string;
}

interface LoginResponse {
    token: string;
    expiresIn: number;
}

interface ApiResponse<T> {
    data: T;
    status: number;
    error?: string;
}

type UserList = User[];

enum UserRole {
    Admin = "admin",
    User = "user",
    ReadOnly = "readonly",
}

// 状态管理
type AppState = {
    user: User | null;
    token: string | null;
    users: UserList;
    error: string | null;
    loading: boolean;
};

// 工具函数
function createUser(data: Partial<User>): User {
    return {
        id: data.id || 0,
        username: data.username || '',
        email: data.email || '',
        role: data.role || UserRole.User,
    };
}

function validateEmail(email: string): boolean {
    return /^[^@]+@[^@]+\.[^@]+$/.test(email);
}

export { User, LoginRequest, LoginResponse, ApiResponse, UserList, UserRole, AppState, createUser, validateEmail };
