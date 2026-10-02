import { useEffect, useRef, useState, type RefObject } from 'react';
import * as THREE from 'three';

export type OrbMode = 'idle' | 'listening' | 'thinking' | 'speaking';

interface ParticleOrbProps {
  /** Live audio level 0..1, read every frame so level changes never re-render. */
  levelRef: RefObject<number>;
  mode: OrbMode;
}

const POINT_COUNT = 1800;
const LINK_DISTANCE = 0.11;
const MAX_LINKS_PER_POINT = 3;

interface ModeStyle {
  color: THREE.Color;
  spin: number;
  ripple: number;
}

const MODE_STYLES: Record<OrbMode, ModeStyle> = {
  idle: { color: new THREE.Color('#2f8fff'), spin: 0.06, ripple: 0.025 },
  listening: { color: new THREE.Color('#3fd4ff'), spin: 0.1, ripple: 0.035 },
  thinking: { color: new THREE.Color('#8a7dff'), spin: 0.55, ripple: 0.07 },
  speaking: { color: new THREE.Color('#5cb8ff'), spin: 0.14, ripple: 0.04 },
};

/** Evenly spread unit vectors over a sphere (golden-angle spiral). */
function fibonacciSphere(count: number): Float32Array {
  const dirs = new Float32Array(count * 3);
  const golden = Math.PI * (3 - Math.sqrt(5));
  for (let i = 0; i < count; i++) {
    const y = 1 - (2 * (i + 0.5)) / count;
    const r = Math.sqrt(1 - y * y);
    const theta = golden * i;
    dirs[i * 3] = Math.cos(theta) * r;
    dirs[i * 3 + 1] = y;
    dirs[i * 3 + 2] = Math.sin(theta) * r;
  }
  return dirs;
}

/** Pairs of nearby points, each point linked to at most a few neighbours. */
function neighbourLinks(dirs: Float32Array, count: number): Uint32Array {
  const pairs: number[] = [];
  const maxSq = LINK_DISTANCE * LINK_DISTANCE;
  for (let i = 0; i < count; i++) {
    const near: Array<[number, number]> = [];
    for (let j = i + 1; j < count; j++) {
      const dx = dirs[i * 3] - dirs[j * 3];
      const dy = dirs[i * 3 + 1] - dirs[j * 3 + 1];
      const dz = dirs[i * 3 + 2] - dirs[j * 3 + 2];
      const d = dx * dx + dy * dy + dz * dz;
      if (d < maxSq) near.push([d, j]);
    }
    near.sort((a, b) => a[0] - b[0]);
    for (const [, j] of near.slice(0, MAX_LINKS_PER_POINT)) pairs.push(i, j);
  }
  return Uint32Array.from(pairs);
}

/** Soft round sprite so points render as glowing dots rather than squares. */
function radialTexture(size: number, stops: Array<[number, string]>): THREE.CanvasTexture {
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext('2d')!;
  const g = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  for (const [at, color] of stops) g.addColorStop(at, color);
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, size, size);
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

/**
 * Fibonacci-distributed points on a unit sphere, joined to their nearest
 * neighbours, displaced along their normals by the voice level. Colour, spin and ripple ease between modes so state
 * changes read as one continuous motion rather than a cut.
 */
export function ParticleOrb({ levelRef, mode }: ParticleOrbProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const modeRef = useRef<OrbMode>(mode);
  const [webglFailed, setWebglFailed] = useState(false);

  useEffect(() => {
    modeRef.current = mode;
  }, [mode]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    } catch {
      setWebglFailed(true);
      return;
    }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setClearColor(0x000000, 0);
    container.appendChild(renderer.domElement);

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(40, 1, 0.1, 100);
    camera.position.set(0, 0, 4.4);

    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const dirs = fibonacciSphere(POINT_COUNT);
    const links = neighbourLinks(dirs, POINT_COUNT);
    // Per-point phase keeps neighbouring particles from moving in lockstep.
    const phase = new Float32Array(POINT_COUNT);
    for (let i = 0; i < POINT_COUNT; i++) phase[i] = Math.random() * Math.PI * 2;

    const positions = new Float32Array(POINT_COUNT * 3);
    const pointGeo = new THREE.BufferGeometry();
    pointGeo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    const dotTexture = radialTexture(64, [
      [0, 'rgba(255,255,255,1)'],
      [0.35, 'rgba(255,255,255,0.55)'],
      [1, 'rgba(255,255,255,0)'],
    ]);
    const pointMat = new THREE.PointsMaterial({
      size: 0.045,
      map: dotTexture,
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
      color: MODE_STYLES.idle.color.clone(),
    });
    const points = new THREE.Points(pointGeo, pointMat);

    const linePositions = new Float32Array(links.length * 3);
    const lineGeo = new THREE.BufferGeometry();
    lineGeo.setAttribute('position', new THREE.BufferAttribute(linePositions, 3));
    const lineMat = new THREE.LineBasicMaterial({
      transparent: true,
      opacity: 0.12,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
      color: MODE_STYLES.idle.color.clone(),
    });
    const lines = new THREE.LineSegments(lineGeo, lineMat);

    const glowTexture = radialTexture(256, [
      [0, 'rgba(255,255,255,0.9)'],
      [0.25, 'rgba(255,255,255,0.35)'],
      [1, 'rgba(255,255,255,0)'],
    ]);
    const glowMat = new THREE.SpriteMaterial({
      map: glowTexture,
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
      color: MODE_STYLES.idle.color.clone(),
      opacity: 0.25,
    });
    const glow = new THREE.Sprite(glowMat);
    glow.scale.setScalar(2.4);

    const orb = new THREE.Group();
    orb.add(points, lines);
    orb.rotation.x = 0.35;
    scene.add(glow, orb);

    const resize = () => {
      const { clientWidth: w, clientHeight: h } = container;
      if (!w || !h) return;
      renderer.setSize(w, h, false);
      renderer.domElement.style.width = `${w}px`;
      renderer.domElement.style.height = `${h}px`;
      camera.aspect = w / h;
      // Keep the whole sphere in view on narrow (portrait) screens.
      camera.position.z = w < h ? 4.4 * (h / w) * 0.8 : 4.4;
      camera.updateProjectionMatrix();
    };
    resize();
    const observer = new ResizeObserver(resize);
    observer.observe(container);

    const clock = new THREE.Clock();
    const color = MODE_STYLES.idle.color.clone();
    const white = new THREE.Color('#ffffff');
    const tmpColor = new THREE.Color();
    let spin = MODE_STYLES.idle.spin;
    let ripple = MODE_STYLES.idle.ripple;
    let level = 0;
    let raf = 0;

    const frame = () => {
      raf = requestAnimationFrame(frame);
      const dt = Math.min(clock.getDelta(), 0.1);
      const t = clock.elapsedTime;
      const style = MODE_STYLES[modeRef.current];

      // Fast attack, slow release: syllables pop, pauses fade out gently.
      const target = Math.max(0, Math.min(1, levelRef.current ?? 0));
      level += (target - level) * (target > level ? 0.35 : 0.08);

      const ease = 1 - Math.exp(-dt * 3);
      spin += (style.spin - spin) * ease;
      ripple += (style.ripple - ripple) * ease;
      color.lerp(style.color, ease);

      const motion = reducedMotion ? 0.3 : 1;
      const breathe = 1 + Math.sin(t * 1.4) * 0.015 * motion;
      const swell = 1 + level * 0.22 * motion;
      const amp = (ripple + level * 0.18) * motion;

      for (let i = 0; i < POINT_COUNT; i++) {
        const x = dirs[i * 3];
        const y = dirs[i * 3 + 1];
        const z = dirs[i * 3 + 2];
        const p = phase[i];
        const wave =
          Math.sin(x * 3.1 + t * 1.3 + p * 0.15) * Math.sin(y * 2.7 + t * 0.9) * Math.sin(z * 3.3 - t * 1.1)
          + 0.5 * Math.sin(x * 6.3 - t * 2.1 + y * 5.1 + p * 0.1);
        const r = (1 + wave * amp) * breathe * swell;
        positions[i * 3] = x * r;
        positions[i * 3 + 1] = y * r;
        positions[i * 3 + 2] = z * r;
      }
      for (let k = 0; k < links.length; k++) {
        const src = links[k] * 3;
        linePositions[k * 3] = positions[src];
        linePositions[k * 3 + 1] = positions[src + 1];
        linePositions[k * 3 + 2] = positions[src + 2];
      }
      pointGeo.attributes.position.needsUpdate = true;
      lineGeo.attributes.position.needsUpdate = true;

      // Loud moments wash the orb towards white-hot.
      tmpColor.copy(color).lerp(white, level * 0.45);
      pointMat.color.copy(tmpColor);
      lineMat.color.copy(tmpColor);
      glowMat.color.copy(color);
      pointMat.size = 0.045 + level * 0.025;
      lineMat.opacity = 0.1 + level * 0.25;
      glowMat.opacity = 0.22 + level * 0.5;
      glow.scale.setScalar(2.4 * swell);

      orb.rotation.y += spin * dt * motion;
      renderer.render(scene, camera);
    };
    raf = requestAnimationFrame(frame);

    return () => {
      cancelAnimationFrame(raf);
      observer.disconnect();
      pointGeo.dispose();
      lineGeo.dispose();
      pointMat.dispose();
      lineMat.dispose();
      glowMat.dispose();
      dotTexture.dispose();
      glowTexture.dispose();
      renderer.dispose();
      renderer.domElement.remove();
    };
  }, [levelRef]);

  if (webglFailed) {
    // No WebGL (disabled GPU, old browser): a CSS pulse keeps the state legible.
    return (
      <div className="w-full h-full flex items-center justify-center">
        <div
          className="rounded-full"
          style={{
            width: 'min(50vmin, 320px)',
            height: 'min(50vmin, 320px)',
            background: 'radial-gradient(circle, rgba(92,184,255,0.55) 0%, rgba(47,143,255,0.15) 55%, transparent 70%)',
            animation: 'pulse 2s ease-in-out infinite',
          }}
        />
      </div>
    );
  }

  return <div ref={containerRef} className="w-full h-full" aria-hidden="true" />;
}
