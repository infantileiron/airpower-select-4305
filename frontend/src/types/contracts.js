/** @typedef {{id:number,hour:string,pressure:number,flow:number,temperature:number,machines:number,sensor_noise:number,kwh:number,note:string,source:string,source_batch_id:number|null,meter_revision:number|null}} Sample */
/** @typedef {{id:number,batch_ref:string,status:string,reason:string,content_hash:string,payload:object[],affected_hours:string[],backfill:number}} MeterBatch */
/** @typedef {{hour:string,reading:number,revision:number,sample_state:string,kwh:number|null,from_hour:string|null,from_reading:number|null,to_reading:number,from_revision:number|null,source_batch_id:number|null,sample_id:number|null}} MeterHour */
/** @typedef {{id:number,name:string,features:string[],alphas:number[],state:string,result:object,error:string}} Run */
/** @typedef {{id:number,run_id:number,title:string,finding:string,action:string,status:string}} Review */
