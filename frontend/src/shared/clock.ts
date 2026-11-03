// Calibrate against HTTP server time; desktop clock changes must not reset an exam.
let base=Date.now();
let tick=performance.now();
export function serverNow() {return base+performance.now()-tick;}
export function observeServerTime(response:Response,started:number) {
  const date=Date.parse(response.headers.get('date')??'');
  if(Number.isFinite(date)){base=date+(performance.now()-started)/2;tick=performance.now();}
}
