package main

import (
	"log"
	"net/http"
	"os"
)

func main() {
	proxyAddr := os.Getenv("SOCKS5_PROXY")
	keysPath := os.Getenv("KEYS_PATH")
	listenAddr := os.Getenv("LISTEN_ADDR")

	if keysPath == "" {
		keysPath = "data/keys.txt"
	}
	if listenAddr == "" {
		listenAddr = ":8080"
	}

	pool := NewKeyPool(keysPath)
	client := NewVapiClient(proxyAddr)
	configStore := NewConfigStore(os.Getenv("CONFIG_PATH"))
	topup := NewTopupManager(pool, configStore)
	topup.StartScheduler()

	mux := http.NewServeMux()
	mux.HandleFunc("GET /", WithAdminAuth(AdminPageHandler(pool, configStore, topup)))
	mux.HandleFunc("GET /admin/login", LoginPageHandler())
	mux.HandleFunc("POST /admin/login", LoginPostHandler())
	mux.HandleFunc("POST /admin/logout", LogoutHandler())
	mux.HandleFunc("GET /admin/state", WithAdminAuth(AdminStateHandler(pool, configStore, topup)))
	mux.HandleFunc("POST /admin/config", WithAdminAuth(AdminConfigHandler(configStore)))
	mux.HandleFunc("POST /admin/topup/run", WithAdminAuth(TopupRunHandler(topup)))
	mux.HandleFunc("POST /admin/topup/stop", WithAdminAuth(TopupStopHandler(topup)))
	mux.HandleFunc("GET /healthz", HealthHandler(pool))
	mux.HandleFunc("POST /v1/chat/completions", WithServerAuth(ChatHandler(pool, client)))
	mux.HandleFunc("GET /v1/models", WithServerAuth(ModelsHandler()))
	mux.HandleFunc("POST /admin/keys/import", WithAdminAuth(ImportHandler(pool)))
	mux.HandleFunc("GET /admin/keys/stats", WithAdminAuth(StatsHandler(pool)))

	stats := pool.Stats()
	log.Printf("[gateway] 启动 addr=%s proxy=%s keys=%d/%d", listenAddr, proxyAddr, stats.Active, stats.Total)
	if err := http.ListenAndServe(listenAddr, mux); err != nil {
		log.Fatal(err)
	}
}
