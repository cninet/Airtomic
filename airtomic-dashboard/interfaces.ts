export interface TelemetryData {
  gw: string;
  node_id: string;
  seq: number;
  temp: number;
  hum: number;
  vib: number;
  dist: number;
  lux: number;
  pir: number;
  rssi: number;
  state: 'AWAKE' | 'SLEEPING' | 'ABSENT';
  conf: number;
  ac: 'ON' | 'OFF';
  light: 'ON' | 'OFF';
  ts?: number; // Firebase server time (ms since 1970) when the gateway wrote this record
}