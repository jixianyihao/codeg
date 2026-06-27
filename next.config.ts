import type { NextConfig } from "next"
import createNextIntlPlugin from "next-intl/plugin"

const isProd = process.env.NODE_ENV === "production"
const internalHost = process.env.TAURI_DEV_HOST || "localhost"
const devServerUrl =
  process.env.CODEG_DEV_SERVER_URL?.replace(/\/+$/, "") ??
  "http://127.0.0.1:3080"
const withNextIntl = createNextIntlPlugin({
  requestConfig: "./src/i18n/request.ts",
  experimental: {
    messages: {
      path: "./src/i18n/messages",
      format: "json",
      locales: [
        "en",
        "zh-CN",
        "zh-TW",
        "ja",
        "ko",
        "es",
        "de",
        "fr",
        "pt",
        "ar",
      ],
      precompile: true,
    },
  },
})

const nextConfig: NextConfig = {
  output: isProd ? "export" : undefined,
  basePath: isProd ? "/__CODEG_BASE_PATH__" : undefined,
  rewrites: isProd
    ? undefined
    : async () => [
        {
          source: "/api/:path*",
          destination: `${devServerUrl}/api/:path*`,
        },
        {
          source: "/ws/:path*",
          destination: `${devServerUrl}/ws/:path*`,
        },
      ],
  images: {
    unoptimized: true,
  },
  assetPrefix: isProd ? undefined : `http://${internalHost}:3000`,
}

export default withNextIntl(nextConfig)
