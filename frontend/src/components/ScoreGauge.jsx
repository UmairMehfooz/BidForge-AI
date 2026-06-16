import { useEffect, useState } from 'react';

export default function ScoreGauge({ score, decision }) {
  const [animatedScore, setAnimatedScore] = useState(0);

  useEffect(() => {
    const timer = setTimeout(() => {
      setAnimatedScore(score);
    }, 100);
    return () => clearTimeout(timer);
  }, [score]);

  const radius = 90;
  const circumference = 2 * Math.PI * radius;
  const strokeDashoffset = circumference - (animatedScore / 100) * circumference;

  let color = '#059669'; // success
  if (decision === 'NO-GO') color = '#E11D48'; // danger
  if (decision === 'CONDITIONAL') color = '#D97706'; // warning

  return (
    <div className="relative flex items-center justify-center w-[200px] h-[200px] mx-auto">
      <svg className="transform -rotate-90 w-full h-full">
        {/* Background Circle */}
        <circle
          cx="100"
          cy="100"
          r={radius}
          stroke="#E9EEF5"
          strokeWidth="12"
          fill="transparent"
        />
        {/* Progress Circle */}
        <circle
          cx="100"
          cy="100"
          r={radius}
          stroke={color}
          strokeWidth="12"
          fill="transparent"
          strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={strokeDashoffset}
          className="transition-all duration-800 ease-out"
          style={{ transitionDuration: '800ms' }}
        />
      </svg>
      <div className="absolute flex flex-col items-center justify-center">
        <span className="text-4xl font-bold font-sans text-brand-heading">{animatedScore}%</span>
      </div>
    </div>
  );
}
