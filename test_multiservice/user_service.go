// user-service.go — 用户服务后端 (Go)

package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"sync"
)

// User 表示用户模型
type User struct {
	ID       int    `json:"id"`
	Username string `json:"username"`
	Email    string `json:"email"`
	Role     string `json:"role"`
}

// UserStore 用户存储接口
type UserStore interface {
	GetUser(id int) (*User, error)
	ListUsers() ([]*User, error)
	CreateUser(user *User) (int, error)
	UpdateUser(user *User) error
	DeleteUser(id int) error
}

// MemoryUserStore 内存用户存储实现
type MemoryUserStore struct {
	mu    sync.RWMutex
	users map[int]*User
	nextID int
}

func NewMemoryUserStore() *MemoryUserStore {
	return &MemoryUserStore{
		users:  make(map[int]*User),
		nextID: 1,
	}
}

func (s *MemoryUserStore) GetUser(id int) (*User, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	user, ok := s.users[id]
	if !ok {
		return nil, fmt.Errorf("user %d not found", id)
	}
	return user, nil
}

func (s *MemoryUserStore) ListUsers() ([]*User, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	result := make([]*User, 0, len(s.users))
	for _, u := range s.users {
		result = append(result, u)
	}
	return result, nil
}

func (s *MemoryUserStore) CreateUser(user *User) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	user.ID = s.nextID
	s.nextID++
	s.users[user.ID] = user
	return user.ID, nil
}

func (s *MemoryUserStore) UpdateUser(user *User) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.users[user.ID]; !ok {
		return fmt.Errorf("user %d not found", user.ID)
	}
	s.users[user.ID] = user
	return nil
}

func (s *MemoryUserStore) DeleteUser(id int) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.users[id]; !ok {
		return fmt.Errorf("user %d not found", id)
	}
	delete(s.users, id)
	return nil
}

// UserHandler HTTP 请求处理器
type UserHandler struct {
	store UserStore
}

func NewUserHandler(store UserStore) *UserHandler {
	return &UserHandler{store: store}
}

func (h *UserHandler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		h.handleGet(w, r)
	case http.MethodPost:
		h.handleCreate(w, r)
	case http.MethodPut:
		h.handleUpdate(w, r)
	case http.MethodDelete:
		h.handleDelete(w, r)
	default:
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	}
}

func (h *UserHandler) handleGet(w http.ResponseWriter, r *http.Request) {
	users, err := h.store.ListUsers()
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	json.NewEncoder(w).Encode(users)
}

func (h *UserHandler) handleCreate(w http.ResponseWriter, r *http.Request) {
	var user User
	if err := json.NewDecoder(r.Body).Decode(&user); err != nil {
		http.Error(w, "invalid JSON", http.StatusBadRequest)
		return
	}
	id, err := h.store.CreateUser(&user)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	user.ID = id
	w.WriteHeader(http.StatusCreated)
	json.NewEncoder(w).Encode(user)
}

func (h *UserHandler) handleUpdate(w http.ResponseWriter, r *http.Request) {
	var user User
	if err := json.NewDecoder(r.Body).Decode(&user); err != nil {
		http.Error(w, "invalid JSON", http.StatusBadRequest)
		return
	}
	if err := h.store.UpdateUser(&user); err != nil {
		http.Error(w, err.Error(), http.StatusNotFound)
		return
	}
	json.NewEncoder(w).Encode(user)
}

func (h *UserHandler) handleDelete(w http.ResponseWriter, r *http.Request) {
	var req struct {
		ID int `json:"id"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid JSON", http.StatusBadRequest)
		return
	}
	if err := h.store.DeleteUser(req.ID); err != nil {
		http.Error(w, err.Error(), http.StatusNotFound)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func main() {
	store := NewMemoryUserStore()
	handler := NewUserHandler(store)

	// 初始化默认用户
	store.CreateUser(&User{Username: "admin", Email: "admin@example.com", Role: "admin"})
	store.CreateUser(&User{Username: "alice", Email: "alice@example.com", Role: "user"})

	fmt.Println("User service running on :8080")
	http.ListenAndServe(":8080", handler)
}
