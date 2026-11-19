'use client';

import React, { useEffect, useState } from 'react';
import {
  Thermometer, Droplets, Sun, Bluetooth,
  Power, AlertTriangle, CheckCircle2, Radio, LogOut
} from 'lucide-react';
import { onAuthStateChanged, signInWithEmailAndPassword, signOut, type User } from 'firebase/auth';
import { onValue, ref, set } from 'firebase/database';
import { auth, db } from '@/firebase';
import { TelemetryData } from '@/interfaces';

export default function AirtomicDashboard() {
  const [user, setUser] = useState<User | null | undefined>(undefined); // undefined = still checking login
  const [nodes, setNodes] = useState<Record<string, TelemetryData>>({});
  const [selected, setSelected] = useState('');
  const [error, setError] = useState('');
  const [isSending, setIsSending] = useState(false);

  useEffect(() => onAuthStateChanged(auth, setUser), []);

  // Live data: the gateway writes nodes/<node_id>; onValue fires on every change.
  useEffect(() => {
    if (!user) return;
    return onValue(
      ref(db, 'nodes'),
      (snap) => {
        setNodes(snap.val() ?? {});
        setError('');
      },
      (err) => setError(err.message),
    );
  }, [user]);

  if (user === undefined) return <Centered>กำลังโหลด...</Centered>;
  if (!user) return <Login />;

  const nodeIds = Object.keys(nodes);
  const data = nodes[selected] ?? nodes[nodeIds[0]];
  if (!data) {
    return (
      <Centered>
        {error ? (
          <div className="text-center space-y-2">
            <p className="text-rose-600 font-medium">อ่านข้อมูลไม่ได้: {error}</p>
            <p className="text-sm">ถ้าเป็น permission denied: เพิ่ม <code>operators/{user.uid}</code> = <code>true</code> ใน Firebase</p>
            <button onClick={() => signOut(auth)} className="text-sm underline">ออกจากระบบ</button>
          </div>
        ) : (
          'รอข้อมูลจาก Gateway...'
        )}
      </Centered>
    );
  }

  // The gateway polls cmd/<node_id>, acts on it, deletes the command, then reports the new state.
  const handleTurnOff = async (target: 'AC' | 'LIGHT') => {
    setIsSending(true);
    try {
      await set(ref(db, `cmd/${data.node_id}/${target.toLowerCase()}`), 'OFF');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setIsSending(false);
    }
  };

  const luxText = data.lux == null ? '-' : data.lux < 0 ? 'ไม่พบเซนเซอร์' : `${data.lux} Lux`;

  const isWastingEnergy = data.state === 'ABSENT' && (data.ac === 'ON' || data.light === 'ON');

  const getStateBadge = (state: TelemetryData['state']) => {
    switch (state) {
      case 'AWAKE':
        return { label: 'มีคน - ตื่นอยู่', color: 'bg-emerald-100 text-emerald-700 border-emerald-300' };
      case 'SLEEPING':
        return { label: 'มีคน - นอนหลับ/นั่งนิ่ง', color: 'bg-blue-100 text-blue-700 border-blue-300' };
      case 'ABSENT':
        return { label: 'ไม่มีคนในห้อง (ABSENT)', color: 'bg-rose-100 text-rose-700 border-rose-300' };
      default: // gateway didn't send a state (e.g. AI not running yet)
        return { label: 'ยังไม่มีผล AI', color: 'bg-slate-200 text-slate-600 border-slate-300' };
    }
  };

  const badge = getStateBadge(data.state);

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900 p-6 md:p-10">
      <div className="max-w-6xl mx-auto space-y-6">
        
        {/* Header (พื้นหลังสีเขียว bg-green-600 คุมโทนเดียวกับการ์ด) */}
        <header className="flex flex-col md:flex-row md:items-center justify-between gap-4 bg-green-600 text-white p-6 rounded-2xl shadow-xs">
          <div>
            <h1 className="text-2xl font-bold tracking-tight text-white">Airtomic Dashboard</h1>
            <p className="text-sm text-green-100">ระบบตรวจจับและป้องกันการลืมปิดไฟ-ปิดแอร์อัจฉริยะ</p>
          </div>
          <div className="flex items-center gap-3 text-xs font-mono bg-green-700/80 text-white px-4 py-2 rounded-lg border border-green-500">
            <Radio className="w-4 h-4 text-emerald-300 animate-pulse" />
            <span>GW: {data.gw}</span>
            <span className="text-green-400">|</span>
            <span>NODE: {data.node_id}</span>
            <span className="text-green-400">|</span>
            <span>SEQ: #{data.seq}</span>
            <span className="text-green-400">|</span>
            <span>อัปเดต {data.ts ? new Date(data.ts).toLocaleTimeString('th-TH') : '-'}</span>
            {nodeIds.length > 1 && (
              <select
                aria-label="เลือก Node"
                value={data.node_id}
                onChange={(e) => setSelected(e.target.value)}
                className="bg-green-800 text-white rounded px-1"
              >
                {nodeIds.map((id) => (
                  <option key={id} value={id}>{id}</option>
                ))}
              </select>
            )}
            <button onClick={() => signOut(auth)} title="ออกจากระบบ" aria-label="ออกจากระบบ">
              <LogOut className="w-4 h-4" />
            </button>
          </div>
        </header>

        {error && (
          <div className="bg-amber-100 border border-amber-200 text-amber-800 rounded-xl p-3 text-sm">{error}</div>
        )}

        {/* Alert Banner */}
        {isWastingEnergy && (
          <div className="bg-rose-100 border border-rose-200 rounded-xl p-4 flex items-center justify-between">
            <div className="flex items-center gap-3">
              <AlertTriangle className="w-6 h-6 text-rose-600 shrink-0" />
              <div>
                <h3 className="font-semibold text-rose-900">ตรวจพบการเปิดอุปกรณ์ทิ้งไว้โดยไม่มีคนอยู่!</h3>
                <p className="text-sm text-rose-700">AI ประเมินว่าไม่มีคนในห้อง (ความมั่นใจ {(data.conf * 100).toFixed(0)}%) กรุณาสั่งปิดอุปกรณ์</p>
              </div>
            </div>
          </div>
        )}

        {/* Top Section */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* AI Status Card */}
          <div className="lg:col-span-2 bg-slate-100 border border-slate-200 rounded-2xl p-6 flex flex-col justify-between">
            <div className="flex items-start justify-between">
              <div>
                <span className="text-xs uppercase tracking-wider text-slate-500 font-semibold">AI Room Classification</span>
                <div className="mt-2 flex items-center gap-3">
                  <span className={`px-3 py-1 rounded-full text-sm font-medium border ${badge.color}`}>
                    {badge.label}
                  </span>
                  <span className="text-sm text-slate-600">
                    Confidence: <strong className="text-slate-900">{(data.conf * 100).toFixed(1)}%</strong>
                  </span>
                </div>
              </div>
              <div className="text-right">
                <span className="text-xs text-slate-500 flex items-center justify-end gap-1">
                  <Bluetooth className="w-3.5 h-3.5 text-sky-600" /> BLE RSSI
                </span>
                <p className="text-lg font-mono font-semibold text-sky-600">{data.rssi} dBm</p>
              </div>
            </div>

            <div className="mt-6 pt-4 border-t border-slate-200 grid grid-cols-3 gap-4 text-sm">
              <div>
                <p className="text-slate-500">PIR Motion</p>
                <p className="font-semibold text-slate-800 mt-0.5">{data.pir === 1 ? 'เคลื่อนไหว' : 'นิ่ง (0)'}</p>
              </div>
              <div>
                <p className="text-slate-500">ระยะหน้าประตู (HC-SR04)</p>
                <p className="font-semibold text-slate-800 mt-0.5">{data.dist} cm</p>
              </div>
              <div>
                <p className="text-slate-500">แรงสั่นแอร์ (SW-420)</p>
                <p className="font-semibold text-slate-800 mt-0.5">{data.vib === 1 ? 'กำลังทำงาน (1)' : 'หยุดนิ่ง (0)'}</p>
              </div>
            </div>
          </div>

          {/* Servo Control Card */}
          <div className="bg-green-600 border border-slate-200 rounded-2xl p-6 flex flex-col justify-between space-y-4">
            <span className="text-xs uppercase tracking-wider text-slate-50 font-semibold">Servo Actuator Control</span>
            
            {/* กล่องควบคุมแอร์ */}
            <div className="flex items-center justify-between bg-slate-100 p-3.5 rounded-2xl border border-slate-200 shadow-xs">
              <div>
                <p className="font-medium text-slate-900">เครื่องปรับอากาศ (AC)</p>
                <p className={`text-xs ${data.ac === 'ON' ? 'text-amber-600 font-medium' : 'text-slate-500'}`}>
                  สถานะ: {data.ac === 'ON' ? 'เปิดอยู่ (ยืนยันจากแรงสั่น)' : 'ปิดแล้ว'}
                </p>
              </div>
              <button
                onClick={() => handleTurnOff('AC')}
                disabled={data.ac === 'OFF' || isSending}
                className="px-4 py-2 rounded-lg text-sm font-medium text-white flex items-center gap-1.5 transition bg-rose-600 hover:bg-rose-500 disabled:bg-slate-300 disabled:text-slate-500"
              >
                <Power className="w-4 h-4" /> สั่งปิด
              </button>
            </div>

            {/* กล่องควบคุมไฟ */}
            <div className="flex items-center justify-between bg-slate-100 p-3.5 rounded-xl border border-slate-200 shadow-xs">
              <div>
                <p className="font-medium text-slate-900">ไฟส่องสว่าง (Light)</p>
                <p className={`text-xs ${data.light === 'ON' ? 'text-amber-600 font-medium' : 'text-slate-400'}`}>
                  สถานะ: {data.light === 'ON' ? `เปิดอยู่ (${luxText})` : 'ปิดแล้ว'}
                </p>
              </div>
              <button
                onClick={() => handleTurnOff('LIGHT')}
                disabled={data.light === 'OFF' || isSending}
                className="px-4 py-2 rounded-lg text-sm font-medium text-white flex items-center gap-1.5 transition bg-rose-600 hover:bg-rose-500 disabled:bg-slate-200 disabled:text-slate-400"
              >
                <Power className="w-4 h-4" /> สั่งปิด
              </button>
            </div>
          </div>
        </div>

        {/* Bottom Section: Environment Sensors */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          <SensorCard icon={<Thermometer className="text-orange-500" />} title="อุณหภูมิหน้าแอร์" value={`${data.temp} °C`} sub="DHT22 (SN01)" />
          <SensorCard icon={<Droplets className="text-cyan-600" />} title="ความชื้นสัมพัทธ์" value={`${data.hum} %`} sub="DHT22 (SN01)" />
          <SensorCard icon={<Sun className="text-amber-500" />} title="ความสว่างในห้อง" value={luxText} sub="BH1750 (GW01)" />
          <SensorCard icon={<CheckCircle2 className="text-emerald-600" />} title="ความปลอดภัยระบบ" value="Privacy-First" sub="No Camera / Non-Invasive" />
        </div>

      </div>
    </div>
  );
}

function Login() {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    try {
      await signInWithEmailAndPassword(auth, email, password);
    } catch (err) {
      setError((err as Error).message);
    }
  };

  return (
    <Centered>
      <form onSubmit={submit} className="bg-white border border-slate-200 rounded-2xl p-6 w-full max-w-sm space-y-3 shadow-xs">
        <h1 className="text-xl font-bold text-green-700">Airtomic Dashboard</h1>
        <input type="email" required aria-label="อีเมล" placeholder="อีเมล" autoComplete="username"
          value={email} onChange={(e) => setEmail(e.target.value)}
          className="w-full border border-slate-300 rounded-lg px-3 py-2" />
        <input type="password" required aria-label="รหัสผ่าน" placeholder="รหัสผ่าน" autoComplete="current-password"
          value={password} onChange={(e) => setPassword(e.target.value)}
          className="w-full border border-slate-300 rounded-lg px-3 py-2" />
        {error && <p className="text-sm text-rose-600">{error}</p>}
        <button type="submit" className="w-full bg-green-600 hover:bg-green-500 text-white rounded-lg py-2 font-medium">
          เข้าสู่ระบบ
        </button>
      </form>
    </Centered>
  );
}

function Centered({ children }: { children: React.ReactNode }) {
  return <div className="min-h-screen bg-slate-50 text-slate-700 flex items-center justify-center p-6">{children}</div>;
}

function SensorCard({ icon, title, value, sub }: { icon: React.ReactNode; title: string; value: string; sub: string }) {
  return (
    <div className="bg-green-600 border border-slate-200 rounded-xl p-5 flex items-center gap-4">
      <div className="p-3 bg-white rounded-lg border border-slate-200 shadow-xs">{icon}</div>
      <div>
        <p className="text-xs text-green-100">{title}</p>
        <p className="text-xl font-bold text-white mt-0.5">{value}</p>
        <p className="text-[11px] text-green-200 mt-0.5">{sub}</p>
      </div>
    </div>
  );
}