export type RasterMime = 'image/png' | 'image/jpeg' | 'image/gif' | 'image/webp'

/** Cheap raster signature/framing pre-filter, NOT a guarantee of complete structure or decodability.
 * Never trust a filename or caller-provided Content-Type. */
export function sniffRasterMime(bytes: Uint8Array): RasterMime | null {
  const has = (offset: number, values: number[]) =>
    offset >= 0 && offset + values.length <= bytes.length && values.every((value, i) => bytes[offset + i] === value)
  if (has(0, [137, 80, 78, 71, 13, 10, 26, 10]) &&
    has(8, [0, 0, 0, 13, 73, 72, 68, 82]) && bytes.length >= 45 &&
    has(bytes.length - 12, [0, 0, 0, 0, 73, 69, 78, 68, 174, 66, 96, 130])) {
    let offset = 8
    while (offset + 12 <= bytes.length) {
      const length = bytes[offset] * 16777216 + bytes[offset + 1] * 65536 + bytes[offset + 2] * 256 + bytes[offset + 3]
      if (length > bytes.length - offset - 12) break
      const end = offset + 12 + length
      if (has(offset + 4, [73, 69, 78, 68])) return length === 0 && end === bytes.length ? 'image/png' : null
      offset = end
    }
  }
  if (bytes.length >= 5 && has(0, [255, 216, 255]) && has(bytes.length - 2, [255, 217])) return 'image/jpeg'
  if (bytes.length >= 14 && (has(0, [71, 73, 70, 56, 55, 97]) || has(0, [71, 73, 70, 56, 57, 97])) &&
    bytes[bytes.length - 1] === 59) return 'image/gif'
  if (bytes.length >= 16 && has(0, [82, 73, 70, 70]) && has(8, [87, 69, 66, 80]) &&
    bytes[4] + bytes[5] * 256 + bytes[6] * 65536 + bytes[7] * 16777216 + 8 === bytes.length &&
    (has(12, [86, 80, 56, 32]) || has(12, [86, 80, 56, 76]) || has(12, [86, 80, 56, 88]))) return 'image/webp'
  return null
}

export type RasterDecoder = (blob: Blob) => Promise<Pick<HTMLImageElement, 'naturalWidth' | 'naturalHeight'>>

/** Use the same decoder as the <img> sinks. Never attach the probe image to the DOM. */
async function defaultDecode(blob: Blob): ReturnType<RasterDecoder> {
  if (typeof Image === 'undefined' || typeof Image.prototype.decode !== 'function') throw new Error('Image.decode unavailable')
  const url = URL.createObjectURL(blob)
  try {
    const image = new Image()
    image.src = url
    await image.decode()
    return image
  } finally {
    URL.revokeObjectURL(url)
  }
}

/** MIME raster detected by signature AND renderable by <img> in this browser
 * (decode with positive natural dimensions); framing alone proves no file integrity. */
export async function verifyRaster(blob: Blob, decode: RasterDecoder = defaultDecode): Promise<RasterMime | null> {
  try {
    const bytes = new Uint8Array(await blob.arrayBuffer())
    const detected = sniffRasterMime(bytes)
    if (!detected) return null
    const image = await decode(new Blob([bytes], { type: detected }))
    return image.naturalWidth > 0 && image.naturalHeight > 0 ? detected : null
  } catch {
    return null
  }
}
