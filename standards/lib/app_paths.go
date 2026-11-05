// Package apppaths provides application path resolution for site projects.
//
// 用途：
//
//	解析應用程式業務路徑（AppRoot、AppConfigDir 等）。
//	優先讀取 APP_* 環境變數，預設值為生產 FHS 路徑。
//
// 使用方式：
//
//	import "github.com/your-org/your-project/src/lib/apppaths"
//
//	fmt.Println(apppaths.AppRoot)
//
// 命名規則替換：
//
//	各專案複製此模板後，可根據業務語義重命名匯出常數。
//	環境變數鍵名（如 "APP_ROOT"）必須與其他語言實作同步替換。
//
//	替換步驟：
//	  1. 複製此目錄至專案的 src/lib/ 目錄，重命名 package 為 <service>paths。
//	  2. 全域取代 App → <Service>（例如 Webservice）。
//	  3. 全域取代 "APP_" → "<SERVICE>_"（例如 "WEBSERVICE_"）。
//	  4. 在 Quadlet .container 的 Environment= 行宣告對應前綴變數。
//
// 注意：
//
//	本 package ≠ site site utilities 的路徑解析（基礎設施層）。
//	本 package 解析應用程式自身業務路徑（應用層）。
//	package-level var 初始化無副作用（不執行 os.MkdirAll 等操作）。
//	回傳型別為 string；消費者可自行包裝為 filepath.Join 或 os.DirFS。
//
// 開發環境覆寫（Dockerfile.dev 注入）：
//
//	APP_ROOT=/workspace
//	APP_CONFIG_DIR=/workspace/config
//	APP_DATA_DIR=/workspace/.data
//	APP_RUNTIME_DIR=/workspace/.run
//	APP_LOG_DIR=/workspace/.logs
package apppaths

import (
	"fmt"
	"os"
)

// getEnvOrDefault returns the value of the environment variable named key,
// or the result of fmt.Sprintf(defaultFmt, args...) if the variable is unset or empty.
func getEnvOrDefault(key, defaultFmt string, args ...any) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fmt.Sprintf(defaultFmt, args...)
}

// projectName returns the PROJECT_NAME env var, defaulting to "myapp".
func projectName() string {
	if n := os.Getenv("PROJECT_NAME"); n != "" {
		return n
	}
	return "myapp"
}

// Exported path variables resolved at program startup.
// All values prefer APP_* environment variables; defaults follow FHS conventions.
//
//nolint:gochecknoglobals // Intentional: package-level path resolution, no side effects.
var (
	// AppRoot is the application installation root. Default: /opt/<PROJECT_NAME>
	AppRoot = getEnvOrDefault("APP_ROOT", "/opt/%s", projectName())

	// AppConfigDir is the application configuration directory. Default: /etc/opt/<PROJECT_NAME>
	AppConfigDir = getEnvOrDefault("APP_CONFIG_DIR", "/etc/opt/%s", projectName())

	// AppDataDir is the application persistent data directory. Default: /var/opt/<PROJECT_NAME>
	AppDataDir = getEnvOrDefault("APP_DATA_DIR", "/var/opt/%s", projectName())

	// AppRuntimeDir is the application runtime socket/pid directory. Default: /run/<PROJECT_NAME>
	AppRuntimeDir = getEnvOrDefault("APP_RUNTIME_DIR", "/run/%s", projectName())

	// AppLogDir is the application log directory. Default: /var/log/<PROJECT_NAME>
	AppLogDir = getEnvOrDefault("APP_LOG_DIR", "/var/log/%s", projectName())

	// AppFrontendDir is the application frontend assets directory. Default: /opt/<PROJECT_NAME>/frontend
	AppFrontendDir = getEnvOrDefault("APP_FRONTEND_DIR", "/opt/%s/frontend", projectName())

	// AppStaticVendorsDir is the application static vendors directory. Default: /opt/<PROJECT_NAME>/static_vendors
	AppStaticVendorsDir = getEnvOrDefault("APP_STATIC_VENDORS_DIR", "/opt/%s/static_vendors", projectName())
)

