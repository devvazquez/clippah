/** @type {import('next').NextConfig} */
// La interfaz no necesita servidor: habla directamente con Supabase, asi que se exporta
// como estatico y se puede subir a cualquier hosting (o abrir desde el disco).
const nextConfig = {
  reactStrictMode: true,
  output: "export",
  images: { unoptimized: true },
  // Con trailingSlash cada ruta es una carpeta con su index.html, que es lo que esperan
  // los hostings estaticos sin reglas de reescritura.
  trailingSlash: true,
};

export default nextConfig;
