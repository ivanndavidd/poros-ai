// Local-only Indonesia choropleth. Province paths are bundled by Vite from the
// pinned @svg-maps/indonesia package: no CDN script, mutable branch, or runtime fetch.
import indonesia from '@svg-maps/indonesia'

const SVG_NS = 'http://www.w3.org/2000/svg'
const ALIAS = {
  PROBANTEN: 'BANTEN',
  'IRIAN JAYA TIMUR': 'PAPUA',
  'IRIAN JAYA TENGAH': 'PAPUA',
  'IRIAN JAYA BARAT': 'PAPUA BARAT',
  'DI ACEH': 'ACEH',
  'NANGGROE ACEH DARUSSALAM': 'ACEH',
  'NUSATENGGARA BARAT': 'NUSA TENGGARA BARAT',
  'BANGKA BELITUNG': 'KEPULAUAN BANGKA BELITUNG',
  'JAKARTA RAYA': 'JAKARTA',
}

function norm(value) {
  const name = String(value || '').toUpperCase()
    .replace(/[.]/g, ' ')
    .replace(/^(PROVINSI|PROV\.?|DAERAH ISTIMEWA|DI|DKI)\s+/, '')
    .replace(/\s+/g, ' ').trim()
  return ALIAS[name] || name
}

function compact(value) {
  return Number(value || 0).toLocaleString('id-ID', { maximumFractionDigits: 2 })
}

function color(value, max) {
  const ratio = Math.max(0, Math.min(1, Number(value || 0) / Math.max(1, max)))
  const lightness = 96 - ratio * 43
  return `hsl(217 91% ${lightness}%)`
}

class IndonesiaChoropleth extends HTMLElement {
  static get observedAttributes() { return ['data', 'title', 'max-label'] }

  connectedCallback() { this.render() }
  attributeChangedCallback() { if (this.isConnected) this.render() }

  values() {
    try {
      const source = JSON.parse(this.getAttribute('data') || '{}')
      return Object.fromEntries(Object.entries(source).map(([key, value]) => [norm(key), Number(value)]))
    } catch {
      return {}
    }
  }

  render() {
    const values = this.values()
    const available = Object.values(values).filter(value => Number.isFinite(value) && value > 0)
    const requestedMax = Number(this.getAttribute('max-label'))
    const max = requestedMax > 0 ? requestedMax : Math.max(...available, 1)

    this.replaceChildren()
    this.style.display = 'block'

    const shell = document.createElement('div')
    shell.style.cssText = 'position:relative;width:100%;background:var(--surface,#fff);border:1px solid var(--border,#e4e8ee);border-radius:11px;overflow:hidden'

    const head = document.createElement('div')
    head.style.cssText = 'display:flex;align-items:center;justify-content:center;position:relative;padding:13px 16px 6px'
    const title = document.createElement('div')
    title.style.cssText = 'font-size:13px;font-weight:600;color:var(--fg,#0f1729);text-align:center'
    title.textContent = this.getAttribute('title') || 'Nilai per Provinsi'
    const png = document.createElement('button')
    png.type = 'button'
    png.textContent = '↓ PNG'
    png.style.cssText = 'position:absolute;right:12px;top:9px;font:inherit;font-size:11px;font-weight:500;color:var(--fg-muted,#5a6475);background:var(--surface,#fff);border:1px solid var(--border,#e4e8ee);border-radius:7px;padding:4px 9px;cursor:pointer'
    head.append(title, png)

    const body = document.createElement('div')
    body.style.cssText = 'position:relative;display:flex;align-items:stretch;padding:4px 8px 10px'
    const legend = document.createElement('div')
    legend.style.cssText = 'flex:0 0 64px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:5px;padding:12px 5px;font-size:10px;color:var(--fg-muted,#5a6475)'
    const high = document.createElement('span'); high.textContent = compact(max)
    const gradient = document.createElement('div')
    gradient.style.cssText = `width:12px;height:100px;border-radius:6px;border:1px solid var(--border,#e4e8ee);background:linear-gradient(to top,${color(0, max)},${color(max, max)})`
    const low = document.createElement('span'); low.textContent = '0'
    legend.append(high, gradient, low)

    const map = document.createElement('div')
    map.style.cssText = 'position:relative;flex:1;min-width:0'
    const svg = document.createElementNS(SVG_NS, 'svg')
    svg.setAttribute('viewBox', indonesia.viewBox)
    svg.setAttribute('role', 'img')
    svg.setAttribute('aria-label', title.textContent)
    svg.style.cssText = 'display:block;width:100%;min-height:240px;max-height:390px;cursor:grab;touch-action:none'
    const group = document.createElementNS(SVG_NS, 'g')
    svg.appendChild(group)

    const tip = document.createElement('div')
    tip.style.cssText = 'position:absolute;pointer-events:none;display:none;background:var(--surface-2,#f1f4f8);border:1px solid var(--border,#e4e8ee);border-radius:8px;padding:7px 10px;font-size:12px;color:var(--fg,#0f1729);box-shadow:0 6px 18px -8px rgba(0,0,0,.3);z-index:5;white-space:nowrap'
    map.append(svg, tip)

    for (const location of indonesia.locations) {
      const province = norm(location.name)
      const value = values[province]
      const hasValue = Number.isFinite(value)
      const path = document.createElementNS(SVG_NS, 'path')
      path.setAttribute('d', location.path)
      path.setAttribute('fill', hasValue ? color(value, max) : '#e9edf1')
      path.setAttribute('stroke', '#fff')
      path.setAttribute('stroke-width', '.55')
      path.style.cursor = 'pointer'
      path.addEventListener('pointerenter', () => {
        path.setAttribute('stroke', hasValue ? '#2563eb' : '#d97706')
        path.setAttribute('stroke-width', '1.5')
        tip.replaceChildren()
        const name = document.createElement('b'); name.textContent = location.name
        const amount = document.createElement('div')
        amount.style.color = 'var(--fg-muted,#5a6475)'
        amount.textContent = hasValue ? compact(value) : 'tidak ada data'
        tip.append(name, amount)
        tip.style.display = 'block'
      })
      path.addEventListener('pointermove', event => {
        const rect = map.getBoundingClientRect()
        tip.style.left = `${Math.min(event.clientX - rect.left + 12, rect.width - tip.offsetWidth - 8)}px`
        tip.style.top = `${Math.max(4, event.clientY - rect.top + 12)}px`
      })
      path.addEventListener('pointerleave', () => {
        path.setAttribute('stroke', '#fff')
        path.setAttribute('stroke-width', '.55')
        tip.style.display = 'none'
      })
      group.appendChild(path)
    }

    let zoom = 1; let tx = 0; let ty = 0; let drag = null
    const transform = () => group.setAttribute('transform', `translate(${tx} ${ty}) scale(${zoom})`)
    const setZoom = next => {
      const previous = zoom
      zoom = Math.max(1, Math.min(8, next))
      const [, , width, height] = indonesia.viewBox.split(/\s+/).map(Number)
      tx -= (zoom - previous) * width / (2 * Math.max(previous, 1))
      ty -= (zoom - previous) * height / (2 * Math.max(previous, 1))
      if (zoom === 1) tx = ty = 0
      transform()
    }
    svg.addEventListener('wheel', event => {
      event.preventDefault()
      setZoom(zoom * (event.deltaY < 0 ? 1.2 : .84))
    }, { passive: false })
    svg.addEventListener('pointerdown', event => {
      drag = { x: event.clientX, y: event.clientY, tx, ty }
      svg.setPointerCapture(event.pointerId); svg.style.cursor = 'grabbing'
    })
    svg.addEventListener('pointermove', event => {
      if (!drag || zoom <= 1) return
      const scale = Number(indonesia.viewBox.split(/\s+/)[2]) / Math.max(1, svg.clientWidth)
      tx = drag.tx + (event.clientX - drag.x) * scale
      ty = drag.ty + (event.clientY - drag.y) * scale
      transform()
    })
    const stopDrag = () => { drag = null; svg.style.cursor = 'grab' }
    svg.addEventListener('pointerup', stopDrag)
    svg.addEventListener('pointercancel', stopDrag)

    const controls = document.createElement('div')
    controls.style.cssText = 'position:absolute;right:8px;bottom:8px;display:flex;gap:4px'
    for (const [label, action] of [['+', () => setZoom(zoom * 1.5)], ['−', () => setZoom(zoom / 1.5)], ['↴', () => { zoom = 1; tx = ty = 0; transform() }]]) {
      const button = document.createElement('button')
      button.type = 'button'; button.textContent = label; button.onclick = action
      button.style.cssText = 'width:28px;height:28px;border:1px solid var(--border,#e4e8ee);border-radius:7px;background:var(--surface,#fff);color:var(--fg,#0f1729);font:inherit;font-size:15px;font-weight:600;cursor:pointer'
      controls.appendChild(button)
    }
    map.appendChild(controls)
    body.append(legend, map)
    shell.append(head, body)
    this.appendChild(shell)

    png.onclick = () => {
      const source = new XMLSerializer().serializeToString(svg)
      const image = new Image()
      image.onload = () => {
        const canvas = document.createElement('canvas')
        canvas.width = 1586; canvas.height = 576
        const context = canvas.getContext('2d')
        context.fillStyle = '#fff'; context.fillRect(0, 0, canvas.width, canvas.height)
        context.drawImage(image, 0, 0, canvas.width, canvas.height)
        const anchor = document.createElement('a')
        anchor.download = 'indonesia-choropleth.png'
        anchor.href = canvas.toDataURL('image/png')
        anchor.click()
      }
      image.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(source)}`
    }
  }
}

if (!customElements.get('indonesia-choropleth')) {
  customElements.define('indonesia-choropleth', IndonesiaChoropleth)
}
