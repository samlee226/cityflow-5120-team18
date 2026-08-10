"use client";

import {motion,useReducedMotion} from "framer-motion";

const blobs=[
  {className:"ambient-blob lavender",x:["0%","15%","6%"],y:["0%","12%","20%"],scale:[1,1.12,.97],duration:52},
  {className:"ambient-blob blue",x:["0%","-16%","-6%"],y:["0%","14%","-7%"],scale:[1.05,.94,1.1],duration:58},
  {className:"ambient-blob pink",x:["0%","12%","-9%"],y:["0%","-17%","-5%"],scale:[.97,1.11,1.01],duration:64},
  {className:"ambient-blob teal",x:["0%","-11%","9%"],y:["0%","-13%","8%"],scale:[1,1.12,.96],duration:60},
  {className:"ambient-blob peach",x:["0%","10%","-7%"],y:["0%","14%","-11%"],scale:[1.06,.95,1.09],duration:68},
];

export default function AmbientBackground(){
  const reduceMotion=useReducedMotion();
  return <div className="ambient-background" aria-hidden="true"><div className="ambient-mesh"/>{blobs.map((blob,index)=><motion.div key={blob.className} className={blob.className} animate={reduceMotion?undefined:{x:blob.x,y:blob.y,scale:blob.scale,rotate:index%2===0?[0,7,-4]:[0,-6,4]}} transition={reduceMotion?undefined:{duration:blob.duration,repeat:Infinity,repeatType:"mirror",ease:"easeInOut"}}/>)}<motion.div className="ambient-wave wave-one" animate={reduceMotion?undefined:{x:["-4%","5%","-2%"],y:["0%","6%","-3%"],rotate:[-8,-3,-10],scale:[1,1.05,.98]}} transition={reduceMotion?undefined:{duration:72,repeat:Infinity,repeatType:"mirror",ease:"easeInOut"}}/><motion.div className="ambient-wave wave-two" animate={reduceMotion?undefined:{x:["3%","-6%","4%"],y:["0%","-5%","4%"],rotate:[14,8,16],scale:[1.04,.97,1.06]}} transition={reduceMotion?undefined:{duration:78,repeat:Infinity,repeatType:"mirror",ease:"easeInOut"}}/><motion.div className="glow-orb orb-one" animate={reduceMotion?undefined:{x:[0,55,-20],y:[0,-35,20],scale:[1,1.15,.95]}} transition={reduceMotion?undefined:{duration:46,repeat:Infinity,repeatType:"mirror",ease:"easeInOut"}}/><motion.div className="glow-orb orb-two" animate={reduceMotion?undefined:{x:[0,-48,24],y:[0,42,-16],scale:[1.08,.94,1.12]}} transition={reduceMotion?undefined:{duration:54,repeat:Infinity,repeatType:"mirror",ease:"easeInOut"}}/><div className="ambient-contrast"/></div>;
}
