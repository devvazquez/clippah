/** @type {import('next').NextConfig} */
const backend = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

const nextConfig = {
  reactStrictMode: true,
  // El frontend habla siempre con rutas relativas /api/*: aqui se proxean al backend
  // para que el SSE y las miniaturas funcionen sin CORS ni URLs absolutas en el cliente.
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${backend}/api/:path*` }];
  },
};

export default nextConfig;
