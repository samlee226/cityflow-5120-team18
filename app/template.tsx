"use client";

import {motion,useReducedMotion} from "framer-motion";

export default function Template({children}:{children:React.ReactNode}){
  const reduceMotion=useReducedMotion();
  return <motion.div initial={reduceMotion?false:{opacity:0}} animate={{opacity:1}} exit={{opacity:0}} transition={{duration:reduceMotion?0:.32,ease:[.22,1,.36,1]}}>{children}</motion.div>;
}
